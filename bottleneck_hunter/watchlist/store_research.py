"""WatchlistStore mixin：复盘 / 经验卡片 / 投资论点 / 三场景估值 / 调优记录 / 回测。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store_base import _now_iso


class _ResearchMixin:
    def create_auto_review(self, sim_trade_id: str, ticker: str,
                           review_type: str = "trade_close",
                           entry_price: float = 0, exit_price: float = 0,
                           return_pct: float = 0, result_json: dict | None = None,
                           lessons_learned: str = "",
                           experience_card: dict | None = None) -> str:
        """写入复盘记录"""
        rid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO auto_reviews
                   (id, sim_trade_id, ticker, review_type, entry_price, exit_price,
                    return_pct, lessons_learned, experience_card, result_json, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (rid, sim_trade_id, ticker, review_type,
                 entry_price, exit_price, return_pct,
                 lessons_learned,
                 json.dumps(experience_card or {}, ensure_ascii=False),
                 json.dumps(result_json or {}, ensure_ascii=False),
                 _now_iso()) + self._user_insert_params() + self._market_insert_params(),
            )
        return rid


    def record_layer_performance(self, trade_id: str, ticker: str,
                                 attribution: dict, return_pct: float) -> None:
        """P1.2 从复盘 attribution 拆出四层归因，写入 layer_performance。

        attribution 结构(来自 trade_review.md):
          stock_selection→L2, market_timing→L3, macro_alignment→L1, plan_deviation→L4
        """
        mapping = {
            "L1": ("macro_alignment", "score"),
            "L2": ("stock_selection", "score"),
            "L3": ("market_timing", "score"),
        }
        rows = []
        for layer, (key, score_field) in mapping.items():
            d = attribution.get(key, {}) or {}
            rows.append((layer, float(d.get(score_field, 0) or 0), d.get("assessment", "")))
        # L4 用 plan_deviation 的偏差绝对值反推质量分(偏差越小越好)
        pd = attribution.get("plan_deviation", {}) or {}
        dev = abs(float(pd.get("entry_diff_pct", 0) or 0)) + abs(float(pd.get("exit_diff_pct", 0) or 0))
        l4_score = max(0.0, 10.0 - dev)  # 偏差 0 → 10 分
        rows.append(("L4", l4_score, pd.get("assessment", "")))

        with self._write_conn() as conn:
            for layer, score, assessment in rows:
                conn.execute(
                    f"""INSERT INTO layer_performance
                       (id, trade_id, ticker, layer, score, assessment, return_pct, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                    (uuid.uuid4().hex[:12], trade_id, ticker, layer, score,
                     assessment, return_pct, _now_iso())
                    + self._user_insert_params() + self._market_insert_params(),
                )


    def get_layer_performance_summary(self, limit: int = 100) -> dict:
        """聚合各层近期表现：平均归因分 + 样本数。返回 {L1:{avg,count}, ...}。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT layer, AVG(score) avg_score, COUNT(*) cnt FROM layer_performance "
                "GROUP BY layer",
            )
            rows = conn.execute(q, p).fetchall()
            out = {}
            for r in rows:
                d = dict(r)
                out[d["layer"]] = {"avg": round(d["avg_score"], 1), "count": d["cnt"]}
            return out
        finally:
            conn.close()


    def get_auto_reviews(self, ticker: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._filtered(
                    "SELECT * FROM auto_reviews WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(
                    "SELECT * FROM auto_reviews ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, dict_fields=("result_json", "experience_card"))
                result.append(d)
            return result
        finally:
            conn.close()


    def get_auto_review(self, review_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM auto_reviews WHERE id = ?", (review_id,)
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            self._parse_json_fields(d, dict_fields=("result_json", "experience_card"))
            return d
        finally:
            conn.close()


    def get_trades_without_review(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT st.* FROM sim_trades st
                   LEFT JOIN auto_reviews ar ON ar.sim_trade_id = st.id
                   WHERE st.side = 'sell' AND ar.id IS NULL
                   ORDER BY st.created_at DESC""",
                table="st",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def create_experience_card(self, scope: str, scope_key: str,
                               category: str, title: str, content: str,
                               evidence: list | None = None,
                               confidence: float = 0.5,
                               source_review_id: str | None = None) -> str:
        """创建经验卡片"""
        cid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO experience_cards
                   (id, scope, scope_key, category, title, content, evidence,
                    confidence, source_review_id, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (cid, scope, scope_key or "", category, title, content,
                 json.dumps(evidence or [], ensure_ascii=False),
                 confidence, source_review_id, _now_iso(), _now_iso()) + self._user_insert_params() + self._market_insert_params(),
            )
        return cid


    def get_experience_cards(self, scope: str | None = None,
                             scope_key: str | None = None,
                             limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            conditions = []
            params: list = []
            if scope:
                conditions.append("scope = ?")
                params.append(scope)
            if scope_key:
                conditions.append("scope_key = ?")
                params.append(scope_key)
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            q, p = self._filtered(
                f"SELECT * FROM experience_cards{where} ORDER BY confidence DESC, applied_count DESC LIMIT ?",
                tuple(params),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()


    def get_relevant_cards(self, ticker: str, sector: str = "",
                           limit: int = 5) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT * FROM experience_cards
                   WHERE (scope = 'global')
                      OR (scope = 'ticker' AND scope_key = ?)
                      OR (scope = 'sector' AND scope_key = ?)
                   ORDER BY confidence DESC, applied_count DESC
                   LIMIT ?""",
                (ticker, sector, limit),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()


    def increment_card_applied(self, card_id: str) -> None:
        """经验卡片被引用时，递增 applied_count 并更新 last_applied_at"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE experience_cards SET applied_count = applied_count + 1, last_applied_at = ?, updated_at = ? WHERE id = ?",
                (_now_iso(), _now_iso(), card_id),
            )
            conn.execute(q, p)


    def update_card_outcome(self, card_id: str, is_win: bool) -> None:
        """根据交易结果更新经验卡片置信度（贝叶斯后验）"""
        with self._write_conn() as conn:
            field = "win_count" if is_win else "loss_count"
            q, p = self._filtered(
                f"UPDATE experience_cards SET {field} = {field} + 1, updated_at = ? WHERE id = ?",
                (_now_iso(), card_id),
            )
            conn.execute(q, p)

            q2, p2 = self._filtered(
                "SELECT win_count, loss_count FROM experience_cards WHERE id = ?",
                (card_id,),
            )
            row = conn.execute(q2, p2).fetchone()
            if row:
                w, l = row["win_count"], row["loss_count"]
                new_conf = round((w + 1) / (w + l + 2), 4)
                q3, p3 = self._filtered(
                    "UPDATE experience_cards SET confidence = ? WHERE id = ?",
                    (new_conf, card_id),
                )
                conn.execute(q3, p3)


    def delete_experience_card(self, card_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._filtered("DELETE FROM experience_cards WHERE id = ?", (card_id,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def create_thesis(self, entry_id: str, ticker: str, title: str,
                      summary: str = "", conviction: str = "medium",
                      time_horizon: str = "medium_term",
                      pillars: list[dict] | None = None) -> str:
        thesis_id = uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO investment_theses
                   (id, entry_id, ticker, thesis_title, thesis_summary,
                    conviction, status, time_horizon, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (thesis_id, entry_id, ticker, title, summary,
                 conviction, "active", time_horizon, now, now) + self._user_insert_params() + self._market_insert_params(),
            )
            for p in (pillars or []):
                pid = uuid.uuid4().hex[:12]
                conn.execute(
                    """INSERT INTO thesis_pillars
                       (id, thesis_id, pillar_text, falsification, weight, status, created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, thesis_id, p.get("text", ""), p.get("falsification", ""),
                     p.get("weight", 1.0), "intact", now),
                )
        return thesis_id


    def get_theses_for_entry(self, entry_id: str, active_only: bool = True) -> list[dict]:
        conn = self._connect()
        try:
            if active_only:
                q, p = self._filtered(
                    "SELECT * FROM investment_theses WHERE entry_id = ? AND status = 'active' ORDER BY created_at DESC",
                    (entry_id,),
                )
            else:
                q, p = self._filtered(
                    "SELECT * FROM investment_theses WHERE entry_id = ? ORDER BY created_at DESC",
                    (entry_id,),
                )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_thesis(self, thesis_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM investment_theses WHERE id = ?", (thesis_id,),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def update_thesis_status(self, thesis_id: str, status: str,
                             conviction: str | None = None) -> bool:
        with self._write_conn() as conn:
            parts = ["status = ?", "updated_at = ?"]
            vals: list = [status, _now_iso()]
            if conviction:
                parts.append("conviction = ?")
                vals.append(conviction)
            if status == "invalidated":
                parts.append("invalidated_at = ?")
                vals.append(_now_iso())
            vals.append(thesis_id)
            q, p = self._filtered(
                f"UPDATE investment_theses SET {', '.join(parts)} WHERE id = ?", tuple(vals),
            )
            return conn.execute(q, p).rowcount > 0


    def get_pillars(self, thesis_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM thesis_pillars WHERE thesis_id = ? ORDER BY weight DESC",
                (thesis_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def update_pillar_status(self, pillar_id: str, status: str) -> bool:
        with self._write_conn() as conn:
            cur = conn.execute(
                "UPDATE thesis_pillars SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now_iso(), pillar_id),
            )
            return cur.rowcount > 0


    def create_evidence(self, thesis_id: str, pillar_id: str | None,
                        date: str, data_point: str,
                        direction: str = "neutral",
                        thesis_impact: str = "no_change",
                        recommended_action: str = "hold",
                        conviction_before: str = "medium",
                        conviction_after: str = "medium",
                        source: str = "") -> str:
        eid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO thesis_evidence_log
                   (id, thesis_id, pillar_id, date, data_point, direction,
                    thesis_impact, recommended_action, conviction_before,
                    conviction_after, source, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (eid, thesis_id, pillar_id, date, data_point, direction,
                 thesis_impact, recommended_action, conviction_before,
                 conviction_after, source, _now_iso()) + self._user_insert_params(),
            )
        return eid


    def get_evidence_log(self, thesis_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            # thesis_evidence_log 无 market 列，且已通过 thesis_id 外键隶属市场隔离的
            # investment_theses，故只做 user 过滤，避免 _market_filter 触发 no such column: market
            q, p = self._user_filter(
                "SELECT * FROM thesis_evidence_log WHERE thesis_id = ? ORDER BY date DESC LIMIT ?",
                (thesis_id, limit),
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_stale_theses(self, days: int = 90) -> list[dict]:
        conn = self._connect()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
            q, p = self._filtered(
                """SELECT * FROM investment_theses
                   WHERE status = 'active' AND updated_at < ?
                   ORDER BY updated_at ASC""",
                (cutoff,),
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_thesis_dashboard(self, entry_id: str | None = None) -> dict:
        conn = self._connect()
        try:
            if entry_id:
                q, p = self._filtered(
                    "SELECT * FROM investment_theses WHERE entry_id = ? AND status = 'active'",
                    (entry_id,),
                )
            else:
                q, p = self._filtered(
                    "SELECT * FROM investment_theses WHERE status = 'active'",
                )
            theses = [dict(r) for r in conn.execute(q, p).fetchall()]
            result = {"theses": [], "total_theses": len(theses),
                      "high": 0, "medium": 0, "low": 0}
            for t in theses:
                tid = t["id"]
                pillars = conn.execute(
                    "SELECT * FROM thesis_pillars WHERE thesis_id = ?", (tid,),
                ).fetchall()
                evidence = conn.execute(
                    "SELECT direction, COUNT(*) as cnt FROM thesis_evidence_log WHERE thesis_id = ? GROUP BY direction",
                    (tid,),
                ).fetchall()
                ev_counts = {r["direction"]: r["cnt"] for r in evidence}
                t["pillars"] = [dict(p) for p in pillars]
                t["pillar_intact"] = sum(1 for p in pillars if p["status"] == "intact")
                t["pillar_total"] = len(pillars)
                t["evidence_supporting"] = ev_counts.get("supporting", 0)
                t["evidence_contradicting"] = ev_counts.get("contradicting", 0)
                t["evidence_neutral"] = ev_counts.get("neutral", 0)
                result["theses"].append(t)
                conv = t.get("conviction", "medium")
                if conv in result:
                    result[conv] += 1
            return result
        finally:
            conn.close()


    def create_scenario_valuation(self, entry_id: str, ticker: str,
                                   strategic_plan_id: str = "",
                                   bear_price: float = 0, bear_probability: float = 0.2,
                                   bear_rationale: str = "",
                                   base_price: float = 0, base_probability: float = 0.6,
                                   base_rationale: str = "",
                                   bull_price: float = 0, bull_probability: float = 0.2,
                                   bull_rationale: str = "",
                                   current_price: float = 0,
                                   valuation_method: str = "relative") -> str:
        vid = uuid.uuid4().hex[:12]
        expected_return = 0.0
        risk_reward = 0.0
        # 归一化概率：如果传入百分比值（和>1），自动转为小数
        prob_sum = bear_probability + base_probability + bull_probability
        if prob_sum > 1.5:
            bear_probability /= 100
            base_probability /= 100
            bull_probability /= 100
        if current_price > 0:
            bear_ret = (bear_price - current_price) / current_price
            base_ret = (base_price - current_price) / current_price
            bull_ret = (bull_price - current_price) / current_price
            expected_return = round(
                bear_ret * bear_probability + base_ret * base_probability + bull_ret * bull_probability, 4
            ) * 100
            downside_ev = abs(bear_ret * bear_probability)
            upside_ev = base_ret * base_probability + bull_ret * bull_probability
            risk_reward = round(upside_ev / downside_ev, 2) if downside_ev > 0 else 0.0
        now = _now_iso()
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO scenario_valuations
                   (id, entry_id, ticker, strategic_plan_id,
                    bear_price, bear_probability, bear_rationale,
                    base_price, base_probability, base_rationale,
                    bull_price, bull_probability, bull_rationale,
                    current_price, expected_return_pct, risk_reward_ratio,
                    valuation_method, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (vid, entry_id, ticker, strategic_plan_id,
                 bear_price, bear_probability, bear_rationale,
                 base_price, base_probability, base_rationale,
                 bull_price, bull_probability, bull_rationale,
                 current_price, expected_return, risk_reward,
                 valuation_method, now, now) + self._user_insert_params() + self._market_insert_params(),
            )
        return vid


    def get_latest_valuation(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM scenario_valuations WHERE entry_id = ? ORDER BY created_at DESC LIMIT 1",
                (entry_id,),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_valuation_history(self, entry_id: str, limit: int = 5) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM scenario_valuations WHERE entry_id = ? ORDER BY created_at DESC LIMIT ?",
                (entry_id, limit),
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_portfolio_valuations(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                """SELECT sv.*, w.company_name, w.tier FROM scenario_valuations sv
                   JOIN watchlist w ON sv.entry_id = w.id
                   WHERE sv.id IN (
                       SELECT id FROM scenario_valuations sv2
                       WHERE sv2.entry_id = sv.entry_id
                       ORDER BY sv2.created_at DESC LIMIT 1
                   )
                   ORDER BY sv.expected_return_pct DESC""",
                table="sv",
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_trade_feedback_history(self, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM trade_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def create_tuning_proposal(self, type_: str, parameter_name: str,
                                old_value: str, new_value: str,
                                reason: str = "", evidence: list | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO tuning_log
                   (id, type, parameter_name, old_value, new_value,
                    reason, evidence, status, proposed_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (tid, type_, parameter_name, old_value, new_value,
                 reason, json.dumps(evidence or [], ensure_ascii=False),
                 "proposed", _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return tid
        finally:
            conn.close()


    def get_tuning_proposals(self, status: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if status:
                q, p = self._user_filter(
                    "SELECT * FROM tuning_log WHERE status = ? ORDER BY proposed_at DESC LIMIT ?",
                    (status, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM tuning_log ORDER BY proposed_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()


    def approve_tuning(self, tuning_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "UPDATE tuning_log SET status = 'approved', decided_at = ? WHERE id = ? AND status = 'proposed'",
                (_now_iso(), tuning_id),
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def reject_tuning(self, tuning_id: str, reason: str = "") -> bool:
        conn = self._connect()
        try:
            if reason:
                q, p = self._user_filter(
                    "UPDATE tuning_log SET status = 'rejected', decided_at = ?, reason = reason || ' | 拒绝: ' || ? WHERE id = ? AND status = 'proposed'",
                    (_now_iso(), reason, tuning_id),
                )
                cur = conn.execute(q, p)
            else:
                q, p = self._user_filter(
                    "UPDATE tuning_log SET status = 'rejected', decided_at = ? WHERE id = ? AND status = 'proposed'",
                    (_now_iso(), tuning_id),
                )
                cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def save_backtest_run(self, run_id: str, start_date: str, end_date: str,
                          initial_capital: float, final_equity: float,
                          total_return_pct: float, sharpe_ratio: float,
                          sortino_ratio: float, max_drawdown_pct: float,
                          calmar_ratio: float, win_rate_pct: float,
                          trade_count: int, equity_curve: list[dict]) -> None:
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO backtest_runs
                   (id, start_date, end_date, initial_capital, final_equity,
                    total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                    calmar_ratio, win_rate_pct, trade_count, equity_curve, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (run_id, start_date, end_date, initial_capital, final_equity,
                 total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                 calmar_ratio, win_rate_pct, trade_count,
                 json.dumps(equity_curve, ensure_ascii=False),
                 _now_iso()) + self._user_insert_params(),
            )


    def get_backtest_runs(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["equity_curve"] = json.loads(d.get("equity_curve", "[]"))
                except (json.JSONDecodeError, TypeError):
                    d["equity_curve"] = []
                results.append(d)
            return results
        finally:
            conn.close()


    def get_backtest_run(self, run_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["equity_curve"] = json.loads(d.get("equity_curve", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["equity_curve"] = []
            return d
        finally:
            conn.close()

