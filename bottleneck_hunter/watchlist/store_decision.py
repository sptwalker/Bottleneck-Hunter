"""WatchlistStore mixin：宏观快照 + 决策引擎 L1-L4 + 被拦截计划前缀。"""

from __future__ import annotations

import json
import logging
import uuid

from bottleneck_hunter.watchlist.snapshot_binding import snapshot_columns
from bottleneck_hunter.watchlist.store_base import _now_iso, _today

logger = logging.getLogger(__name__)


class _DecisionMixin:
    def save_macro_snapshot(self, indicator: str, date: str, value: float,
                           fetched_at: str | None = None, change_pct: float = 0.0) -> None:
        with self._write_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO macro_snapshots
                   (date, indicator, value, change_pct, fetched_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (date, indicator, value, change_pct, fetched_at or _now_iso()),
            )


    def get_latest_macro_snapshots(self) -> list[dict]:
        """返回每个指标最新一条记录。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT indicator, value, change_pct, date, fetched_at
                   FROM macro_snapshots
                   WHERE (indicator, date) IN (
                       SELECT indicator, MAX(date) FROM macro_snapshots GROUP BY indicator
                   )"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_macro_snapshot_history(self, indicator: str, days: int = 30) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM macro_snapshots WHERE indicator = ? ORDER BY date DESC LIMIT ?",
                (indicator, days),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_decision_center_stats(self) -> dict:
        """决策中心运行统计 —— 按当前用户、跨两市场汇总（战术计划额外按市场拆分）。

        全程只用 _user_filter（不绑定 market），故 A股/美股合并计数；唯战术计划 GROUP BY market
        给出「两个市场分别」的拆分。所有涉及的表(macro_strategies/strategic_plans/tactical_plans/
        execution_plans/committee_consensus/sim_trades/experience_cards/catalyst_tracking)均已具
        user_id 列，_user_filter 追加 AND user_id=? 保证不串用户；未绑定用户则退化为空过滤(计 0，
        这些均非 VIP 专属表故不触发 G-5 护栏)。
        """
        conn = self._connect()
        try:
            def _count(sql: str, params: tuple = ()) -> int:
                q, p = self._user_filter(sql, params)
                row = conn.execute(q, p).fetchone()
                return int(row[0]) if row and row[0] is not None else 0

            # 投委会终裁词表见 committee.py：approved / approved_with_modifications=通过，rejected=否决，
            # needs_review=须人工复核（法定人数不足，结论不可背书），needs_discussion=待议（僵持未决）。
            # P2-F（N-17/N-18）：needs_review 单独成列。原先它与 needs_discussion 一起被压进
            # 「待议=总数-通过-否决」这一个灰格里，于是「委员会没能表决」（须人来看）和「委员吵架了」
            # （正常流程）在统计上长得完全一样 —— 前者是缺陷信号，被后者稀释到看不见。
            committee_total = _count("SELECT COUNT(*) FROM committee_consensus")
            approved = _count(
                "SELECT COUNT(*) FROM committee_consensus "
                "WHERE final_verdict IN ('approved', 'approved_with_modifications')"
            )
            rejected = _count("SELECT COUNT(*) FROM committee_consensus WHERE final_verdict = 'rejected'")
            needs_review = _count("SELECT COUNT(*) FROM committee_consensus WHERE final_verdict = 'needs_review'")

            # 战术计划按市场拆分（_user_filter 在 GROUP BY 前插 user_id 过滤）
            q, p = self._user_filter("SELECT market, COUNT(*) FROM tactical_plans GROUP BY market")
            tactical_by_market = {(r[0] or "us_stock"): int(r[1]) for r in conn.execute(q, p).fetchall()}

            # 「从有记录可查开始」= 最早一条 L1 宏观(漏斗之首)的创建时间；created_at 为 UTC 可字典序 MIN
            q, p = self._user_filter("SELECT MIN(created_at) FROM macro_strategies")
            row = conn.execute(q, p).fetchone()
            since = row[0] if row and row[0] else None

            return {
                "since": since,
                "macro_rounds": _count("SELECT COUNT(*) FROM macro_strategies"),
                "strategic": _count("SELECT COUNT(*) FROM strategic_plans"),
                "tactical_total": sum(tactical_by_market.values()),
                "tactical_by_market": tactical_by_market,
                "execution": _count("SELECT COUNT(*) FROM execution_plans"),
                "committee_total": committee_total,
                "committee_approved": approved,
                "committee_rejected": rejected,
                "committee_needs_review": needs_review,
                "committee_pending": max(0, committee_total - approved - rejected),
                "trades": _count("SELECT COUNT(*) FROM sim_trades"),
                "experiences": _count("SELECT COUNT(*) FROM experience_cards"),
                "catalysts": _count("SELECT COUNT(*) FROM catalyst_tracking"),
            }
        finally:
            conn.close()


    def create_macro_strategy(self, result_json: dict, *, snapshot_id: str | None = None,
                              strategy_version: str | None = None, strict: bool = True) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id, strategy_version=strategy_version, strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM macro_strategies"
            )
            version = conn.execute(q, p).fetchone()[0]
            q, p = self._filtered(
                "UPDATE macro_strategies SET status = 'superseded' WHERE status = 'valid'"
            )
            conn.execute(q, p)
            now = _now_iso()
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO macro_strategies
                   (id, version, regime, risk_appetite, recommended_cash_pct,
                    market_summary, key_signals, sector_rotation, risk_factors,
                    strategy_text, valid_until_trigger, result_json, status, created_at, updated_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                           {vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    sid, version,
                    rj.get("regime", "sideways"),
                    rj.get("risk_appetite", "balanced"),
                    rj.get("recommended_cash_pct", 25.0),
                    rj.get("market_summary", ""),
                    json.dumps(rj.get("key_signals", []), ensure_ascii=False),
                    json.dumps(rj.get("sector_rotation", {}), ensure_ascii=False),
                    json.dumps(rj.get("risk_factors", []), ensure_ascii=False),
                    rj.get("strategy_text", ""),
                    rj.get("valid_until_trigger", ""),
                    json.dumps(rj, ensure_ascii=False),
                    "valid", now, now,
                ) + params + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()


    def get_latest_macro_strategy(self) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM macro_strategies WHERE status = 'valid' ORDER BY version DESC LIMIT 1"
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                q, p = self._filtered(
                    "SELECT * FROM macro_strategies ORDER BY version DESC LIMIT 1"
                )
                row = conn.execute(q, p).fetchone()
            return self._parse_macro_row(row) if row else None
        finally:
            conn.close()


    def get_macro_history(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT id, version, regime, risk_appetite, market_summary,
                   status, created_at, updated_at, snapshot_id, strategy_version
                   FROM macro_strategies ORDER BY version DESC LIMIT ?""",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def update_macro_status(self, strategy_id: str, status: str,
                            minor_tweaks: list | None = None,
                            daily_commentary: str | None = None) -> bool:
        """日检回写：status 直接落列，`minor_tweaks` / `daily_commentary` 打进 `result_json`。

        **为什么不给 `daily_commentary` 单开一列**：本表 `valid_until_trigger` 的 N-19 注释已定调
        ——"别为同一份数据接第二个消费方，那会造成改了一处、另一处仍陈旧的分裂"。`result_json`
        本就是 L1 的完整落点（前端 `renderMacro` 读它、L2 prompt 由它渲染），再开一列就是那个副本。

        ponytail: `daily_commentary` 每次日检**覆盖**，只留最新一句、不能逐日回看。要日检史就建
        `macro_daily_checks` 表（DDL 见 docs/TRADING_DECISION_SYSTEM.md 3.2 节），届时本方法
        改写成往那张表 INSERT；在那之前别在 `result_json` 里堆数组模拟历史。
        """
        conn = self._connect()
        try:
            parts = ["status = ?", "updated_at = ?"]
            vals = [status, _now_iso()]
            if minor_tweaks is not None or daily_commentary is not None:
                q, p = self._filtered(
                    "SELECT result_json FROM macro_strategies WHERE id = ?", (strategy_id,)
                )
                row = conn.execute(q, p).fetchone()
                if row:
                    rj = json.loads(row["result_json"] or "{}")
                    # 仅在传了时才写：日检返回 minor_tweaks=None（常态）不得把上一次的微调抹掉
                    if minor_tweaks is not None:
                        rj["minor_tweaks"] = minor_tweaks
                    # 空串同样不写：LLM 没给点评时保留上一条，好过用空串把有内容的盖成空
                    if daily_commentary:
                        rj["daily_commentary"] = daily_commentary
                    parts.append("result_json = ?")
                    vals.append(json.dumps(rj, ensure_ascii=False))
            vals.append(strategy_id)
            q, p = self._filtered(
                f"UPDATE macro_strategies SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def _parse_macro_row(self, row) -> dict:
        return self._parse_json_fields(
            dict(row),
            dict_fields=("sector_rotation", "result_json"),
            list_fields=("key_signals", "risk_factors"),
        )


    def create_strategic_plan(self, macro_strategy_id: str, result_json: dict, *,
                              snapshot_id: str | None = None, strategy_version: str | None = None,
                              strict: bool = True) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id, strategy_version=strategy_version, strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategic_plans"
            )
            version = conn.execute(q, p).fetchone()[0]
            q, p = self._filtered(
                "UPDATE strategic_plans SET status = 'superseded' WHERE status = 'valid'"
            )
            conn.execute(q, p)
            now = _now_iso()
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO strategic_plans
                   (id, macro_strategy_id, version, overall_stance, target_allocation,
                    sector_targets, stock_selection, risk_limits, rebalancing_triggers,
                    strategy_text, result_json, status, created_at, updated_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?
                           {vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    sid, macro_strategy_id, version,
                    rj.get("overall_stance", "balanced"),
                    json.dumps(rj.get("target_allocation", {}), ensure_ascii=False),
                    json.dumps(rj.get("sector_targets", {}), ensure_ascii=False),
                    json.dumps(rj.get("stock_selection", {}), ensure_ascii=False),
                    json.dumps(rj.get("risk_limits", {}), ensure_ascii=False),
                    json.dumps(rj.get("rebalancing_triggers", []), ensure_ascii=False),
                    rj.get("strategy_text", ""),
                    json.dumps(rj, ensure_ascii=False),
                    "valid", now, now,
                ) + params + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()


    def get_latest_strategic_plan(self) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM strategic_plans WHERE status = 'valid' ORDER BY version DESC LIMIT 1"
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                q, p = self._filtered(
                    "SELECT * FROM strategic_plans ORDER BY version DESC LIMIT 1"
                )
                row = conn.execute(q, p).fetchone()
            return self._parse_strategic_row(row) if row else None
        finally:
            conn.close()


    def get_strategic_history(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT id, macro_strategy_id, version, overall_stance,
                   status, created_at, updated_at, snapshot_id, strategy_version
                   FROM strategic_plans ORDER BY version DESC LIMIT ?""",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def _parse_strategic_row(self, row) -> dict:
        return self._parse_json_fields(
            dict(row),
            dict_fields=("target_allocation", "sector_targets", "stock_selection",
                         "risk_limits", "result_json"),
            list_fields=("rebalancing_triggers",),
        )


    def create_tactical_plan(self, strategic_plan_id: str, entry_id: str,
                             ticker: str, plan_date: str, result_json: dict, *,
                             snapshot_id: str | None = None, strategy_version: str | None = None,
                             strict: bool = True) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id, strategy_version=strategy_version, strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO tactical_plans
                   (id, strategic_plan_id, entry_id, ticker, plan_date, action,
                    entry_plan, exit_plan, catalyst_watch, confidence,
                    result_json, status, created_at, updated_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?
                           {vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    sid, strategic_plan_id, entry_id, ticker, plan_date,
                    rj.get("action", "hold"),
                    json.dumps(rj.get("entry_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("exit_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("catalyst_watch", []), ensure_ascii=False),
                    rj.get("confidence", 5),
                    json.dumps(rj, ensure_ascii=False),
                    "active", _now_iso(), _now_iso(),
                ) + params + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()


    def get_tactical_plans_by_date(self, plan_date: str | None = None) -> list[dict]:
        plan_date = plan_date or _today()
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM tactical_plans WHERE plan_date = ? ORDER BY confidence DESC",
                (plan_date,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("entry_plan", "exit_plan", "result_json"),
                                            ("catalyst_watch",)) for r in rows]
        finally:
            conn.close()


    def delete_tactical_plans_by_date(self, plan_date: str, only_active: bool = True) -> int:
        """删除指定日期的战术计划（默认仅 active），返回删除行数。

        L3 重新生成前调用，避免「日常决策 / 全量刷新 / 定时任务 / 重复点击」
        在同一 plan_date 下累积重复战术计划。已执行(executed)的计划默认保留。
        """
        conn = self._connect()
        try:
            sql = "DELETE FROM tactical_plans WHERE plan_date = ?"
            if only_active:
                sql += " AND status = 'active'"
            q, p = self._filtered(sql, (plan_date,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


    def get_tactical_plan_for_ticker(self, ticker: str, plan_date: str | None = None) -> dict | None:
        plan_date = plan_date or _today()
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM tactical_plans WHERE ticker = ? AND plan_date = ? AND status = 'active' LIMIT 1",
                (ticker, plan_date),
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            return self._parse_json_fields(dict(row), ("entry_plan", "exit_plan", "result_json"),
                                           ("catalyst_watch",))
        finally:
            conn.close()


    def get_latest_tactical_plan_for_ticker(self, ticker: str) -> dict | None:
        """该 ticker 最近一条 active 战术计划（不限当天）——供硬止损巡检跨天读取止损位。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM tactical_plans WHERE ticker = ? AND status = 'active' "
                "ORDER BY plan_date DESC LIMIT 1",
                (ticker,),
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            return self._parse_json_fields(dict(row), ("entry_plan", "exit_plan", "result_json"),
                                           ("catalyst_watch",))
        finally:
            conn.close()


    def create_execution_plan(self, tactical_plan_id: str, entry_id: str,
                              ticker: str, result_json: dict,
                              status: str = "pending",
                              rejection_reason: str = "", *, snapshot_id: str | None = None,
                              strategy_version: str | None = None, strict: bool = True) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id, strategy_version=strategy_version, strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO execution_plans
                   (id, tactical_plan_id, entry_id, ticker, action, shares,
                    target_price, amount, method, priority, confidence,
                    reasoning, result_json, status, rejection_reason, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                           {vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    sid, tactical_plan_id, entry_id, ticker,
                    rj.get("action", "hold"),
                    rj.get("shares", 0),
                    rj.get("target_price") or rj.get("estimated_price"),
                    rj.get("amount", 0) or rj.get("estimated_amount", 0),
                    rj.get("method") or rj.get("execution_method", "market"),
                    rj.get("priority", 5) if isinstance(rj.get("priority"), int) else 5,
                    rj.get("confidence", 5),
                    rj.get("reasoning") or rj.get("rationale", ""),
                    json.dumps(rj, ensure_ascii=False),
                    status, rejection_reason, _now_iso(),
                ) + params + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()


    def create_blocked_execution(self, tactical_plan_id: str, entry_id: str,
                                 ticker: str, result_json: dict,
                                 reason: str, marker: str = "[系统拦截]", *, snapshot_id: str | None = None,
                                 strategy_version: str | None = None, strict: bool = True) -> str:
        """创建被拦截的执行计划(status=rejected + 标记)，并写入 trade_feedback 回灌决策。"""
        full_reason = f"{marker} {reason}"
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id, strategy_version=strategy_version, strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        sid = uuid.uuid4().hex[:12]
        fid = uuid.uuid4().hex[:12]
        rj = result_json or {}
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO execution_plans
                   (id, tactical_plan_id, entry_id, ticker, action, shares,
                    target_price, amount, method, priority, confidence,
                    reasoning, result_json, status, rejection_reason, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                           {vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (sid, tactical_plan_id, entry_id, ticker, rj.get("action", "hold"),
                 rj.get("shares", 0), rj.get("target_price") or rj.get("estimated_price"),
                 rj.get("amount", 0) or rj.get("estimated_amount", 0),
                 rj.get("method") or rj.get("execution_method", "market"),
                 rj.get("priority", 5) if isinstance(rj.get("priority"), int) else 5,
                 rj.get("confidence", 5), rj.get("reasoning") or rj.get("rationale", ""),
                 json.dumps(rj, ensure_ascii=False), "rejected", full_reason, _now_iso())
                + params + self._user_insert_params() + self._market_insert_params(),
            )
            conn.execute(
                f"""INSERT INTO trade_feedback
                   (id, execution_plan_id, ticker, feedback_type, reason, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?{vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (fid, sid, ticker, "auto_block", full_reason, _now_iso())
                + params + self._user_insert_params() + self._market_insert_params(),
            )
        return sid


    def get_blocked_executions(self, limit: int = 50) -> list[dict]:
        """获取被系统/投委会拦截的执行计划(供前端'已拦截'区展示)。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM execution_plans WHERE status = 'rejected' "
                "AND (rejection_reason LIKE ? OR rejection_reason LIKE ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (f"{self.BLOCK_MARKER_SYSTEM}%", f"{self.BLOCK_MARKER_COMMITTEE}%", limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()


    def restore_execution(self, plan_id: str) -> bool:
        """将被拦截的计划恢复为 pending(用户手动 override)。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'pending', rejection_reason = '' "
                "WHERE id = ? AND status = 'rejected'",
                (plan_id,),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def apply_committee_modifications(self, plan_id: str, modifications: dict) -> bool:
        """P0.5 应用投委会共识修改到执行计划(缩量/调价/改方式)。

        modifications 支持键: shares, target_price, method/execution_method。
        """
        plan = self.get_execution_plan(plan_id)
        if not plan:
            return False
        rj = plan.get("result_json", {})
        if not isinstance(rj, dict):
            rj = {}
        new_shares = modifications.get("shares")
        new_price = modifications.get("target_price") or modifications.get("limit_price")
        new_method = modifications.get("method") or modifications.get("execution_method")

        # 投委会只准「缩量」：放大 shares 会绕过 L4 原始约束校验（现金/占比/额度），
        # 生成超约束的待确认指令。放大 / 原计划无股数却要加量 → 一律钳回原值。防非数值崩溃。
        cur_shares = plan.get("shares") or 0
        if new_shares is not None:
            try:
                _new, _cur = float(new_shares), float(cur_shares)
            except (TypeError, ValueError):
                _new = _cur = 0.0
            if _cur <= 0 or _new > _cur:
                logger.info("投委会改单放大被拒 %s: %s→%s，钳回原值", plan_id, cur_shares, new_shares)
                new_shares = cur_shares

        if new_shares is not None:
            rj["shares"] = new_shares
        if new_price is not None:
            rj["target_price"] = new_price
        if new_method is not None:
            rj["execution_method"] = new_method
        rj["committee_modified"] = True

        with self._write_conn() as conn:
            sets = ["result_json = ?"]
            vals: list = [json.dumps(rj, ensure_ascii=False)]
            if new_shares is not None:
                sets.append("shares = ?")
                vals.append(new_shares)
            if new_price is not None:
                sets.append("target_price = ?")
                vals.append(new_price)
            if new_method is not None:
                sets.append("method = ?")
                vals.append(new_method)
            q, p = self._filtered(
                f"UPDATE execution_plans SET {', '.join(sets)} WHERE id = ? AND status = 'pending'",
                tuple(vals) + (plan_id,),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def get_pending_executions(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM execution_plans WHERE status = 'pending' ORDER BY priority ASC, created_at ASC"
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()


    def confirm_execution(self, plan_id: str) -> bool:
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'",
                (_now_iso(), plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def revert_to_pending(self, plan_id: str) -> bool:
        """P2.2 执行失败回滚：把 confirmed 退回 pending(状态机加固，避免卡死)。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'pending', confirmed_at = NULL "
                "WHERE id = ? AND status = 'confirmed'",
                (plan_id,),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def record_execution_failure(self, plan_id: str, error: str) -> bool:
        """P0-B（N-2）：某次成交尝试失败 → 累加计数 + 记最后错误。

        失败计划会被 revert_to_pending 滚回 pending，行状态与「刚生成还没轮到」完全一样——
        没有计数就只能靠猜。有了 attempt_count，pending 老化任务与「失败 N 次」告警才分得清
        「从未尝试」和「反复失败」。
        """
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET attempt_count = COALESCE(attempt_count, 0) + 1, "
                "last_error = ? WHERE id = ?",
                ((error or "")[:500], plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def get_stale_pending_executions(self, cutoff_iso: str, limit: int = 500) -> list[dict]:
        """P0-B：长期滞留的待确认计划（早于 cutoff、且非挂单）。供老化任务收尸。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM execution_plans WHERE status = 'pending' "
                "AND COALESCE(resting_until, '') = '' AND created_at < ? "
                "ORDER BY created_at LIMIT ?",
                (cutoff_iso, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()

    def expire_stale_pending(self, plan_id: str, reason: str) -> bool:
        """P0-B：把长期滞留的 pending 计划置为 expired（收尸）。

        与 expire_execution 刻意分开：后者要求 status='confirmed' AND resting_until 非空（挂单专用），
        对从未成交过的 pending 计划恒为假——这正是「pending 堆积却无人收尸」的根因所在。
        """
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'expired', rejection_reason = ? "
                "WHERE id = ? AND status = 'pending' AND COALESCE(resting_until, '') = ''",
                (reason, plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def reject_execution(self, plan_id: str, reason: str = "") -> bool:
        with self._write_conn() as conn:
            q, p = self._filtered("SELECT * FROM execution_plans WHERE id = ?", (plan_id,))
            row = conn.execute(q, p).fetchone()
            if not row or row["status"] not in ("pending", "confirmed"):
                return False
            plan_market = row["market"] or self._market or "us_stock"
            # 端点可能未 scope；按计划市场重 scope，但始终保留当前用户过滤，不能用计划 user_id 重绑。
            if row["snapshot_id"] is not None or row["strategy_version"] is not None:
                from bottleneck_hunter.watchlist.snapshot_binding import bind_snapshot

                bind_snapshot(
                    snapshot_id=row["snapshot_id"],
                    strategy_version=row["strategy_version"],
                    strict=True,
                    get_snapshot=self.for_market(plan_market).get_research_snapshot,
                )
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'rejected', rejection_reason = ? "
                "WHERE id = ? AND status IN ('pending', 'confirmed')",
                (reason, plan_id),
            )
            cur = conn.execute(q, p)
            if cur.rowcount > 0:
                conn.execute(
                    """INSERT INTO trade_feedback
                       (id, execution_plan_id, ticker, feedback_type, reason, market, created_at,
                        user_id, snapshot_id, strategy_version)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex[:12], plan_id, row["ticker"], "rejection",
                     reason, plan_market, _now_iso(), row["user_id"], row["snapshot_id"], row["strategy_version"]),
                )
            return cur.rowcount > 0


    def clear_pending_executions(self) -> int:
        """清空所有 pending/confirmed(未执行) 的执行计划，标记为 rejected。挂单(resting)不清。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'rejected', rejection_reason = '用户手动清空' "
                "WHERE status IN ('pending', 'confirmed') AND executed_at IS NULL "
                "AND COALESCE(resting_until, '') = ''"
            )
            cur = conn.execute(q, p)
            return cur.rowcount


    # ── 挂单交易（限价单）生命周期：resting_until 非空 = 挂单中 ──────────────
    def rest_execution(self, plan_id: str, resting_until: str) -> bool:
        """把已确认(confirmed)的计划转为挂单：首次落挂单时间/到期，重复调用不重置(避免续期)。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET method = 'limit', "
                "resting_since = CASE WHEN COALESCE(resting_since,'')='' THEN ? ELSE resting_since END, "
                "resting_until = CASE WHEN COALESCE(resting_until,'')='' THEN ? ELSE resting_until END "
                "WHERE id = ? AND status = 'confirmed'",
                (_now_iso(), resting_until, plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def get_resting_executions(self, limit: int = 100) -> list[dict]:
        """挂单中的限价单（confirmed + resting_until 非空）。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM execution_plans WHERE status = 'confirmed' "
                "AND COALESCE(resting_until,'') != '' ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()

    def get_recent_executed(self, limit: int = 20) -> list[dict]:
        """近期已成交的执行计划（status=executed，按成交时间倒序）——自动执行开启时 L4 栏据此展示，避免空白。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM execution_plans WHERE status = 'executed' "
                "ORDER BY executed_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()

    def mark_executed(self, plan_id: str) -> bool:
        """成交成功 → 推进到 executed（激活状态机死状态），清挂单标记。普通成交与挂单成交共用。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'executed', executed_at = ?, resting_until = '' "
                "WHERE id = ? AND status = 'confirmed'",
                (_now_iso(), plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def expire_execution(self, plan_id: str, reason: str = "") -> bool:
        """挂单到期/用户取消 → expired，清挂单标记（仅作用于挂单中的计划）。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'expired', resting_until = '', rejection_reason = ? "
                "WHERE id = ? AND status = 'confirmed' AND COALESCE(resting_until,'') != ''",
                (reason, plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def unclaim_execution(self, plan_id: str, resting_until: str = "") -> bool:
        """成交失败回滚：executed→confirmed，恢复 executed_at 与原挂单标记
        （配合 execute_trade 的原子领单：mark_executed 抢占后若成交失败则补偿回滚）。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'confirmed', executed_at = NULL, resting_until = ? "
                "WHERE id = ? AND status = 'executed'",
                (resting_until or "", plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def get_execution_plan(self, plan_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM execution_plans WHERE id = ?", (plan_id,))
            row = conn.execute(q, p).fetchone()
            return self._parse_json_fields(dict(row), ("result_json",)) if row else None
        finally:
            conn.close()

