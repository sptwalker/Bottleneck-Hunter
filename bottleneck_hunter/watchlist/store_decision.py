"""WatchlistStore mixin：宏观快照 + 决策引擎 L1-L4 + 被拦截计划前缀。"""

from __future__ import annotations

import json
import uuid
import logging

logger = logging.getLogger(__name__)

from bottleneck_hunter.watchlist.store_base import _now_iso, _today


class _DecisionMixin:
    def save_macro_snapshot(self, indicator: str, date: str, value: float,
                           fetched_at: str | None = None) -> None:
        with self._write_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO macro_snapshots
                   (date, indicator, value, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (date, indicator, value, fetched_at or _now_iso()),
            )


    def get_latest_macro_snapshots(self) -> list[dict]:
        """返回每个指标最新一条记录。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT indicator, value, date, fetched_at
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


    def create_macro_strategy(self, result_json: dict) -> str:
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
                    strategy_text, valid_until_trigger, result_json, status, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
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
                ) + self._user_insert_params() + self._market_insert_params(),
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
                   status, created_at, updated_at
                   FROM macro_strategies ORDER BY version DESC LIMIT ?""",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def update_macro_status(self, strategy_id: str, status: str,
                            minor_tweaks: list | None = None) -> bool:
        conn = self._connect()
        try:
            parts = ["status = ?", "updated_at = ?"]
            vals = [status, _now_iso()]
            if minor_tweaks is not None:
                q, p = self._filtered(
                    "SELECT result_json FROM macro_strategies WHERE id = ?", (strategy_id,)
                )
                row = conn.execute(q, p).fetchone()
                if row:
                    rj = json.loads(row["result_json"] or "{}")
                    rj["minor_tweaks"] = minor_tweaks
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


    def create_strategic_plan(self, macro_strategy_id: str, result_json: dict) -> str:
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
                    strategy_text, result_json, status, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
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
                ) + self._user_insert_params() + self._market_insert_params(),
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
                   status, created_at, updated_at
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
                             ticker: str, plan_date: str, result_json: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO tactical_plans
                   (id, strategic_plan_id, entry_id, ticker, plan_date, action,
                    entry_plan, exit_plan, catalyst_watch, confidence,
                    result_json, status, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    sid, strategic_plan_id, entry_id, ticker, plan_date,
                    rj.get("action", "hold"),
                    json.dumps(rj.get("entry_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("exit_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("catalyst_watch", []), ensure_ascii=False),
                    rj.get("confidence", 5),
                    json.dumps(rj, ensure_ascii=False),
                    "active", _now_iso(), _now_iso(),
                ) + self._user_insert_params() + self._market_insert_params(),
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
                              rejection_reason: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO execution_plans
                   (id, tactical_plan_id, entry_id, ticker, action, shares,
                    target_price, amount, method, priority, confidence,
                    reasoning, result_json, status, rejection_reason, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
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
                ) + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()


    def create_blocked_execution(self, tactical_plan_id: str, entry_id: str,
                                 ticker: str, result_json: dict,
                                 reason: str, marker: str = "[系统拦截]") -> str:
        """创建被拦截的执行计划(status=rejected + 标记)，并写入 trade_feedback 回灌决策。"""
        full_reason = f"{marker} {reason}"
        sid = self.create_execution_plan(
            tactical_plan_id=tactical_plan_id, entry_id=entry_id,
            ticker=ticker, result_json=result_json,
            status="rejected", rejection_reason=full_reason,
        )
        try:
            self.create_trade_feedback(
                execution_plan_id=sid, ticker=ticker,
                feedback_type="auto_block", reason=full_reason,
            )
        except Exception:
            logger.debug("create_trade_feedback failed for blocked %s", ticker)
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
                sets.append("shares = ?"); vals.append(new_shares)
            if new_price is not None:
                sets.append("target_price = ?"); vals.append(new_price)
            if new_method is not None:
                sets.append("method = ?"); vals.append(new_method)
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
                "UPDATE execution_plans SET status = 'pending', confirmed_at = NULL WHERE id = ? AND status = 'confirmed'",
                (plan_id,),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def reject_execution(self, plan_id: str, reason: str = "") -> bool:
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'rejected', rejection_reason = ? WHERE id = ? AND status IN ('pending', 'confirmed')",
                (reason, plan_id),
            )
            cur = conn.execute(q, p)
            if cur.rowcount > 0:
                q2, p2 = self._filtered("SELECT ticker FROM execution_plans WHERE id = ?", (plan_id,))
                row = conn.execute(q2, p2).fetchone()
                if row:
                    conn.execute(
                        f"""INSERT INTO trade_feedback
                           (id, execution_plan_id, ticker, feedback_type, reason, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                           VALUES (?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                        (uuid.uuid4().hex[:12], plan_id, row["ticker"], "rejection",
                         reason, _now_iso()) + self._user_insert_params() + self._market_insert_params(),
                    )
            return cur.rowcount > 0


    def clear_pending_executions(self) -> int:
        """清空所有 pending/confirmed(未执行) 的执行计划，标记为 rejected。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE execution_plans SET status = 'rejected', rejection_reason = '用户手动清空' "
                "WHERE status IN ('pending', 'confirmed') AND executed_at IS NULL"
            )
            cur = conn.execute(q, p)
            return cur.rowcount


    def get_execution_plan(self, plan_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM execution_plans WHERE id = ?", (plan_id,))
            row = conn.execute(q, p).fetchone()
            return self._parse_json_fields(dict(row), ("result_json",)) if row else None
        finally:
            conn.close()

