"""WatchlistStore mixin：投委会评审 / 交易反馈。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.snapshot_binding import snapshot_columns
from bottleneck_hunter.watchlist.store_base import _now_iso, _today


class _CommitteeMixin:
    def create_committee_review(
        self,
        execution_plan_id: str,
        member_role: str,
        model_provider: str,
        model_name: str,
        result_json: dict,
        *,
        snapshot_id: str | None = None,
        strategy_version: str | None = None,
        strict: bool = True,
    ) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id,
            strategy_version=strategy_version,
            strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        rid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO committee_reviews
                   (id, execution_plan_id, member_role, model_provider, model_name,
                    vote, confidence, score, key_concerns, suggestions,
                    result_json, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    rid,
                    execution_plan_id,
                    member_role,
                    model_provider,
                    model_name,
                    rj.get("vote", "approve"),
                    rj.get("confidence", 5),
                    rj.get("score")
                    or rj.get("risk_score")
                    or rj.get("growth_score")
                    or rj.get("value_score")
                    or rj.get("contrarian_score"),
                    json.dumps(rj.get("key_concerns", []), ensure_ascii=False),
                    json.dumps(rj.get("suggestions", []), ensure_ascii=False),
                    json.dumps(rj, ensure_ascii=False),
                    _now_iso(),
                )
                + params
                + self._user_insert_params()
                + self._market_insert_params(),
            )
            conn.commit()
            return rid
        finally:
            conn.close()

    def get_reviews_for_execution(self, execution_plan_id: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM committee_reviews WHERE execution_plan_id = ? ORDER BY created_at",
                (execution_plan_id,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",), ("key_concerns", "suggestions")) for r in rows]
        finally:
            conn.close()

    def get_committee_consensus(self, execution_plan_id: str) -> dict | None:
        """取某执行计划最近一次投委会共识（无则 None）。P0-A：自动执行前的独立判据。

        投委会 gating 与自动执行是两条路径（后者还能被 revert_to_pending 等状态机操作绕过），
        故自动执行不能只看 status=pending，必须回读共识里的 final_verdict 亲自背书。
        """
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM committee_consensus WHERE execution_plan_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (execution_plan_id,),
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            return self._parse_json_fields(
                dict(row), ("result_json", "vote_detail"),
                ("consensus_modifications", "final_execution_plan", "key_risks_flagged", "minority_opinions"),
            )
        finally:
            conn.close()

    def create_committee_consensus(
        self,
        execution_plan_id: str,
        result_json: dict,
        *,
        snapshot_id: str | None = None,
        strategy_version: str | None = None,
        strict: bool = True,
    ) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id,
            strategy_version=strategy_version,
            strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        cid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO committee_consensus
                   (id, execution_plan_id, final_verdict, approval_rate,
                    vote_detail, consensus_modifications, final_execution_plan,
                    key_risks_flagged, minority_opinions, summary, result_json, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    cid,
                    execution_plan_id,
                    rj.get("final_verdict", "approved"),
                    rj.get("approval_rate", 0.0),
                    json.dumps(rj.get("vote_detail", {}), ensure_ascii=False),
                    json.dumps(rj.get("consensus_modifications", []), ensure_ascii=False),
                    json.dumps(rj.get("final_execution_plan", []), ensure_ascii=False),
                    json.dumps(rj.get("key_risks_flagged", []), ensure_ascii=False),
                    json.dumps(rj.get("minority_opinions", []), ensure_ascii=False),
                    rj.get("summary", ""),
                    json.dumps(rj, ensure_ascii=False),
                    _now_iso(),
                )
                + params
                + self._user_insert_params()
                + self._market_insert_params(),
            )
            conn.commit()
            return cid
        finally:
            conn.close()

    def create_reverse_analysis(
        self,
        *,
        ticker: str,
        company_name: str = "",
        company_name_cn: str = "",
        sector: str = "",
        bottleneck_node: str = "",
        quality_score: float = 0.0,
        alpha_score: float = 0.0,
        final_score: float = 0.0,
        source: str = "llm",
        matched_analysis_id: str = "",
        owner_analysis_id: str = "",
        result_json: dict | None = None,
    ) -> str:
        """落库一条反向分析结果。result_json 存完整 SupplierScorecard（前端详情据此渲染）。

        owner_analysis_id: 发起本次反向分析时用户所在的正向分析记录 id，使每条正向记录
        拥有各自独立的反向分析列表（区别于语义不同、常为空的 matched_analysis_id）。
        """
        rid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO reverse_analyses
                   (id, ticker, company_name, company_name_cn, sector, bottleneck_node,
                    quality_score, alpha_score, final_score, source, matched_analysis_id,
                    owner_analysis_id, result_json, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    rid,
                    ticker,
                    company_name,
                    company_name_cn,
                    sector,
                    bottleneck_node,
                    quality_score,
                    alpha_score,
                    final_score,
                    source,
                    matched_analysis_id,
                    owner_analysis_id,
                    json.dumps(result_json or {}, ensure_ascii=False, default=str),
                    _now_iso(),
                    _now_iso(),
                )
                + self._user_insert_params()
                + self._market_insert_params(),
            )
            conn.commit()
            return rid
        finally:
            conn.close()

    def list_reverse_analyses(self, limit: int = 100, owner_analysis_id: str | None = None) -> list[dict]:
        """列表（不含 result_json，轻量）。按当前 user + market 过滤。

        owner_analysis_id 非空时，仅返回归属该正向分析记录的反向分析（每条记录独立列表）。
        为 None/空时回退旧行为（返回该 user+market 下全部），兼容后台/脚本直查。
        """
        conn = self._connect()
        try:
            cols = (
                "SELECT id, ticker, company_name, company_name_cn, market, sector, "
                "bottleneck_node, quality_score, alpha_score, final_score, source, "
                "matched_analysis_id, owner_analysis_id, created_at, updated_at "
                "FROM reverse_analyses"
            )
            if owner_analysis_id:
                # owner 条件放进 base WHERE，_filtered 会以 AND 追加 user_id + market
                q, p = self._filtered(cols + " WHERE owner_analysis_id = ?", (owner_analysis_id,))
            else:
                q, p = self._filtered(cols)
            q += " ORDER BY created_at DESC LIMIT ?"
            p = p + (limit,)
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_reverse_analysis(self, analysis_id: str) -> dict | None:
        """单条完整记录（含解析后的 result_json）。"""
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM reverse_analyses WHERE id = ?", (analysis_id,))
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            return self._parse_json_fields(dict(row), ("result_json",))
        finally:
            conn.close()

    def delete_reverse_analysis(self, analysis_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._filtered("DELETE FROM reverse_analyses WHERE id = ?", (analysis_id,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def create_catalyst(
        self,
        entry_id: str,
        ticker: str,
        title: str,
        catalyst_type: str = "event",
        description: str = "",
        expected_date: str | None = None,
        impact_level: str = "medium",
        confidence: int = 5,
        source_category: str = "other",
        impact_color: str = "yellow",
        direction: str = "neutral",
        time_window: str = "",
        position_implication: str = "",
    ) -> str:
        cid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO catalyst_tracking
                   (id, entry_id, ticker, catalyst_type, title, description,
                    expected_date, impact_level, confidence, status,
                    source_category, impact_color, direction, time_window, position_implication,
                    created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (
                    cid,
                    entry_id,
                    ticker,
                    catalyst_type,
                    title,
                    description,
                    expected_date,
                    impact_level,
                    confidence,
                    "pending",
                    source_category,
                    impact_color,
                    direction,
                    time_window,
                    position_implication,
                    _now_iso(),
                    _now_iso(),
                )
                + self._user_insert_params()
                + self._market_insert_params(),
            )
            conn.commit()
            return cid
        finally:
            conn.close()

    def get_catalysts_for_entry(self, entry_id: str, active_only: bool = True) -> list[dict]:
        conn = self._connect()
        try:
            if active_only:
                q, p = self._filtered(
                    "SELECT * FROM catalyst_tracking WHERE entry_id = ? AND status IN ('pending','monitoring') ORDER BY expected_date",
                    (entry_id,),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(
                    "SELECT * FROM catalyst_tracking WHERE entry_id = ? ORDER BY created_at DESC",
                    (entry_id,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_catalyst_status(
        self, catalyst_id: str, status: str, outcome: str = "", actual_date: str | None = None
    ) -> bool:
        conn = self._connect()
        try:
            parts = ["status = ?", "updated_at = ?"]
            vals: list = [status, _now_iso()]
            if outcome:
                parts.append("outcome = ?")
                vals.append(outcome)
            if actual_date:
                parts.append("actual_date = ?")
                vals.append(actual_date)
            vals.append(catalyst_id)
            q, p = self._filtered(f"UPDATE catalyst_tracking SET {', '.join(parts)} WHERE id = ?", tuple(vals))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_catalysts_for_ticker(self, ticker: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM catalyst_tracking WHERE ticker = ? ORDER BY created_at DESC",
                (ticker,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_upcoming_catalysts(self, days: int = 14) -> list[dict]:
        conn = self._connect()
        try:
            cutoff = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
            q, p = self._filtered(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status IN ('pending','monitoring')
                   AND ct.expected_date IS NOT NULL
                   AND ct.expected_date <= ?
                   ORDER BY ct.expected_date ASC""",
                (cutoff,),
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_valuation_map(self) -> dict[str, dict]:
        """{归一 ticker: 最新一条 scenario_valuation}——批量版 get_latest_valuation（避免逐票 N 次查询）。

        P0-4 信念驱动器的输入：一次取全市场最新估值（内含 prob 加权的 expected_return_pct），
        按 created_at 倒序扫、每票只留第一条即最新。
        """
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM scenario_valuations ORDER BY created_at DESC",
            )
            out: dict[str, dict] = {}
            for r in conn.execute(q, p).fetchall():
                d = dict(r)
                tk = d.get("ticker", "")
                if tk and tk not in out:
                    out[tk] = d
            return out
        finally:
            conn.close()

    def get_recent_catalysts(self, days_ahead: int = 14, days_back: int = 7, limit: int = 8) -> list[dict]:
        """论坛发帖取「近期热点事件」用：即将发生（pending/monitoring 且 expected_date 在未来
        days_ahead 内）+ 近期已触发（triggered 且 updated_at 在过去 days_back 内），按最近活动排序。

        与 get_upcoming_catalysts 的区别：额外带「已触发」以贴合「发现新问题」。市场未绑定时
        （论坛店 _market=''）_filtered 跳过市场过滤 → 跨市场取，正是留言板想要的。
        """
        conn = self._connect()
        try:
            now = datetime.now(timezone.utc)
            ahead = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
            back = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S")
            q, p = self._filtered(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE (
                       (ct.status IN ('pending','monitoring')
                        AND ct.expected_date IS NOT NULL AND ct.expected_date <= ?)
                       OR (ct.status = 'triggered' AND ct.updated_at >= ?)
                   )
                   ORDER BY COALESCE(ct.updated_at, ct.created_at) DESC""",
                (ahead, back),
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows][:limit]
        finally:
            conn.close()

    def expire_past_catalysts(self) -> int:
        conn = self._connect()
        try:
            today = _today()
            q, p = self._filtered(
                """UPDATE catalyst_tracking SET status = 'expired', updated_at = ?
                   WHERE expected_date < ? AND status IN ('pending', 'monitoring')""",
                (_now_iso(), today),
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def get_expiring_catalysts(self, days: int = 7) -> list[dict]:
        conn = self._connect()
        try:
            today = _today()
            future = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
            q, p = self._filtered(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status IN ('pending', 'monitoring')
                   AND ct.expected_date IS NOT NULL
                   AND ct.expected_date >= ? AND ct.expected_date <= ?
                   ORDER BY ct.expected_date ASC""",
                (today, future),
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_unjudged_expired_catalysts(self) -> list[dict]:
        """获取已过期但未判定结果的催化剂"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status = 'expired'
                   AND (ct.outcome IS NULL OR ct.outcome = '')
                   ORDER BY ct.expected_date DESC""",
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def judge_catalyst(self, catalyst_id: str, outcome: str, impact: float, actual_date: str | None = None) -> bool:
        conn = self._connect()
        try:
            parts = ["outcome = ?", "outcome_impact = ?", "judged_at = ?", "updated_at = ?"]
            now = _now_iso()
            vals: list = [outcome, impact, now, now]
            if actual_date:
                parts.append("actual_date = ?")
                vals.append(actual_date)
            # 如果 realized，同步更新 status 为 triggered
            if outcome == "realized":
                parts.append("status = ?")
                vals.append("triggered")
            vals.append(catalyst_id)
            q, p = self._filtered(f"UPDATE catalyst_tracking SET {', '.join(parts)} WHERE id = ?", tuple(vals))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_recently_judged_catalysts(self, days: int = 7) -> list[dict]:
        """P1.1 获取最近判定结果的催化剂(realized/failed/partial)，供 L3 生成买卖信号。"""
        conn = self._connect()
        try:
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            q, p = self._filtered(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.outcome IN ('realized','failed','partial')
                   AND ct.judged_at IS NOT NULL AND ct.judged_at >= ?
                   ORDER BY ct.judged_at DESC""",
                (since,),
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_trade_feedback(
        self,
        execution_plan_id: str,
        ticker: str,
        feedback_type: str = "rejection",
        reason: str = "",
        user_note: str = "",
        *,
        snapshot_id: str | None = None,
        strategy_version: str | None = None,
        strict: bool = True,
    ) -> str:
        cols, vals, params = snapshot_columns(
            snapshot_id=snapshot_id,
            strategy_version=strategy_version,
            strict=strict,
            get_snapshot=self.get_research_snapshot,
        )
        fid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO trade_feedback
                   (id, execution_plan_id, ticker, feedback_type, reason, user_note, created_at
                    {cols}{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?{vals}{self._user_insert_vals()}{self._market_insert_vals()})""",
                (fid, execution_plan_id, ticker, feedback_type, reason, user_note, _now_iso())
                + params
                + self._user_insert_params()
                + self._market_insert_params(),
            )
            conn.commit()
            return fid
        finally:
            conn.close()

    def get_committee_rejection_summary(self, days: int = 30, min_count: int = 2,
                                        limit: int = 5) -> list[dict]:
        """P2-D（N-15）：投委会**非结论/否决**裁决按标的聚合，供 L1 日检"看得见"上下游分歧。

        N-15 的病：投委会的裁决到不了 L1。同一标的一直被否、而 L1 仍写着"超配"，两边持续
        相互抵消，每轮烧一次全链算力，用户只看到"一直建议买、一直不执行"两套说辞。
        这里只做**留痕聚合**，不自动改 regime —— 让下游改写上游的宏观判断，风险远大于收益。

        聚到标的（ticker）而非板块：`rejection_patterns` 是 `trade_feedback` 的裸 SELECT，
        而生产上 A 股 watchlist 的 `sector` 全空、美股 21 条里只有 15 条有值，**按板块聚合会
        把大部分记录静默丢进 NULL 桶**（实测：直方图里 None 桶比任何真板块都大）。`sector`
        作为附带字段带出，有值才显示——能给出板块视角，但不把聚合正确性压在它上面。

        只算 `rejected` / `needs_review`：这两种是"委员会说了不"。`needs_discussion` 是僵持
        未决，算成"被否"会把"还在吵"记成"已经否掉"。
        """
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT ep.ticker AS ticker, ep.action AS action, MAX(w.sector) AS sector,
                          COUNT(DISTINCT cc.id) AS rejects, MAX(cc.created_at) AS last_at
                   FROM committee_consensus cc
                   JOIN execution_plans ep ON ep.id = cc.execution_plan_id
                   LEFT JOIN watchlist w ON w.ticker = ep.ticker AND w.market = cc.market
                   WHERE cc.final_verdict IN ('rejected', 'needs_review')
                     AND cc.created_at >= date('now', ?)
                   GROUP BY ep.ticker, ep.action
                   HAVING COUNT(DISTINCT cc.id) >= ?
                   ORDER BY rejects DESC, last_at DESC
                   LIMIT ?""",
                # 参数化为日期修饰符（而非拼接 SQL 字面量），天数由调用方定、不接受外部字符串
                (f"-{int(days)} days", max(1, int(min_count)), max(1, int(limit))),
                table="cc",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_rejection_patterns(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._filtered(
                    "SELECT * FROM trade_feedback WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(
                    "SELECT * FROM trade_feedback ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
