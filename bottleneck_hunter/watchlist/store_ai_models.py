"""WatchlistStore mixin：AI 模型预测 / 综合评分 / 角色配置。"""

from __future__ import annotations

import json
import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _AIModelsMixin:
    def record_prediction(self, *, provider: str, model: str, role_context: str,
                          ticker: str, prediction_type: str, prediction_value: str,
                          market: str = "") -> str:
        rid = str(uuid.uuid4())
        now = _now_iso()
        mkt = market or self._market or "us_stock"
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO model_accuracy
                       (id, model_provider, model_name, role_context, ticker,
                        prediction_type, prediction_value, prediction_date,
                        created_at, user_id, market)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, provider, model, role_context, ticker,
                     prediction_type, prediction_value, now[:10],
                     now, self._user_id, mkt),
                )
                conn.commit()
            finally:
                conn.close()
        return rid


    def record_outcome(self, ticker: str, prediction_type: str,
                       outcome_value: str, outcome_date: str = "",
                       score_delta: float = 0.0) -> int:
        now = _now_iso()
        odate = outcome_date or now[:10]
        with self._write_lock:
            conn = self._connect()
            try:
                is_correct = 1 if abs(score_delta) < 2.0 else 0
                q = """UPDATE model_accuracy SET outcome_value = ?, outcome_date = ?,
                       is_correct = ?, score_delta = ?, updated_at = ?
                       WHERE ticker = ? AND prediction_type = ? AND is_correct = -1"""
                params = (outcome_value, odate, is_correct, score_delta, now,
                          ticker, prediction_type)
                if self._user_id:
                    q += " AND user_id = ?"
                    params = params + (self._user_id,)
                cur = conn.execute(q, params)
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()


    def get_model_accuracy(self, provider: str, model: str,
                           role_context: str | None = None,
                           limit: int = 100, market: str = "") -> list[dict]:
        conn = self._connect()
        try:
            q = "SELECT * FROM model_accuracy WHERE model_provider = ? AND model_name = ?"
            p: tuple = (provider, model)
            if role_context is not None:
                q += " AND role_context = ?"
                p = p + (role_context,)
            if market:
                q += " AND market = ?"   # 按市场过滤，避免近期准确率跨市场混算污染校准
                p = p + (market,)
            q += " ORDER BY prediction_date DESC LIMIT ?"
            p = p + (limit,)
            q, p = self._user_filter(q, p)
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_model_accuracy_stats(self, market: str = "") -> list[dict]:
        conn = self._connect()
        try:
            q = """SELECT model_provider, model_name, role_context,
                   COUNT(*) as total,
                   SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct,
                   SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as incorrect,
                   SUM(CASE WHEN is_correct = -1 THEN 1 ELSE 0 END) as pending,
                   AVG(CASE WHEN is_correct >= 0 THEN score_delta END) as avg_delta
                   FROM model_accuracy"""
            p: tuple = ()
            if market:
                q += " WHERE market = ?"
                p = (market,)
            q, p = self._user_filter(q, p)
            q += " GROUP BY model_provider, model_name, role_context ORDER BY total DESC"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def get_model_ratings(self, role_context: str | None = None,
                          market: str = "") -> list[dict]:
        conn = self._connect()
        try:
            q = "SELECT * FROM model_ratings"
            p: tuple = ()
            conditions = []
            if role_context is not None:
                conditions.append("role_context = ?")
                p = p + (role_context,)
            if market:
                conditions.append("market = ?")
                p = p + (market,)
            if conditions:
                q += " WHERE " + " AND ".join(conditions)
            q, p = self._user_filter(q, p)
            q += " ORDER BY calibration_weight DESC"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def upsert_model_rating(self, *, provider: str, model: str,
                            role_context: str = "", total: int = 0,
                            correct: int = 0, accuracy: float = 0.5,
                            avg_delta: float = 0.0, weight: float = 1.0,
                            market: str = "") -> None:
        now = _now_iso()
        mkt = market or self._market or "us_stock"
        uid = self._user_id or ""
        with self._write_lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    """SELECT id FROM model_ratings
                       WHERE model_provider = ? AND model_name = ?
                       AND role_context = ? AND user_id = ? AND market = ?""",
                    (provider, model, role_context, uid, mkt),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE model_ratings SET total_predictions = ?,
                           correct_predictions = ?, accuracy_rate = ?,
                           avg_score_delta = ?, calibration_weight = ?,
                           last_calibrated = ?, updated_at = ?
                           WHERE id = ?""",
                        (total, correct, accuracy, avg_delta, weight, now, now,
                         existing["id"]),
                    )
                else:
                    conn.execute(
                        """INSERT INTO model_ratings
                           (id, model_provider, model_name, role_context,
                            total_predictions, correct_predictions, accuracy_rate,
                            avg_score_delta, calibration_weight, last_calibrated,
                            created_at, updated_at, user_id, market)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), provider, model, role_context,
                         total, correct, accuracy, avg_delta, weight, now,
                         now, now, uid, mkt),
                    )
                conn.commit()
            finally:
                conn.close()


    def get_calibration_weight(self, provider: str, model: str,
                               role_context: str = "", market: str = "") -> float:
        conn = self._connect()
        try:
            mkt = market or self._market or "us_stock"
            uid = self._user_id or ""
            row = conn.execute(
                """SELECT calibration_weight FROM model_ratings
                   WHERE model_provider = ? AND model_name = ?
                   AND role_context = ? AND user_id = ? AND market = ?""",
                (provider, model, role_context, uid, mkt),
            ).fetchone()
            return row["calibration_weight"] if row else 1.0
        finally:
            conn.close()


    def create_meeting_record(self, *, meeting_type: str, title: str,
                              participants: list | None = None,
                              tickers_discussed: list | None = None,
                              final_verdict: str = "",
                              final_ranking: list | None = None,
                              key_agreements: list | None = None,
                              key_disagreements: list | None = None,
                              risk_warnings: list | None = None,
                              investment_thesis: str = "",
                              transcript_json: list | None = None,
                              result_json: dict | None = None,
                              model_predictions: list | None = None,
                              duration_seconds: int = 0,
                              total_tokens: int = 0,
                              analysis_id: str = "",
                              execution_plan_id: str = "",
                              market: str = "") -> str:
        rid = str(uuid.uuid4())
        now = _now_iso()
        mkt = market or self._market or "us_stock"
        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute(
                    """INSERT INTO meeting_records
                       (id, meeting_type, analysis_id, execution_plan_id, market,
                        title, participants, tickers_discussed,
                        final_verdict, final_ranking, key_agreements,
                        key_disagreements, risk_warnings, investment_thesis,
                        transcript_json, result_json, model_predictions,
                        duration_seconds, total_tokens, created_at, user_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid, meeting_type, analysis_id, execution_plan_id, mkt,
                     title,
                     json.dumps(participants or [], ensure_ascii=False),
                     json.dumps(tickers_discussed or [], ensure_ascii=False),
                     final_verdict,
                     json.dumps(final_ranking or [], ensure_ascii=False),
                     json.dumps(key_agreements or [], ensure_ascii=False),
                     json.dumps(key_disagreements or [], ensure_ascii=False),
                     json.dumps(risk_warnings or [], ensure_ascii=False),
                     investment_thesis,
                     json.dumps(transcript_json or [], ensure_ascii=False),
                     json.dumps(result_json or {}, ensure_ascii=False),
                     json.dumps(model_predictions or [], ensure_ascii=False),
                     duration_seconds, total_tokens, now, self._user_id or ""),
                )
                conn.commit()
            finally:
                conn.close()
        return rid


    def get_meeting_records(self, meeting_type: str | None = None,
                            market: str = "", limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q = "SELECT * FROM meeting_records"
            p: tuple = ()
            conditions = []
            if meeting_type:
                conditions.append("meeting_type = ?")
                p = p + (meeting_type,)
            if market:
                conditions.append("market = ?")
                p = p + (market,)
            if conditions:
                q += " WHERE " + " AND ".join(conditions)
            q, p = self._user_filter(q, p)
            q += " ORDER BY created_at DESC LIMIT ?"
            p = p + (limit,)
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d,
                    dict_fields=("result_json",),
                    list_fields=("participants", "tickers_discussed",
                                 "final_ranking", "key_agreements",
                                 "key_disagreements", "risk_warnings",
                                 "transcript_json", "model_predictions"))
                result.append(d)
            return result
        finally:
            conn.close()


    def get_meeting_record(self, record_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM meeting_records WHERE id = ?", (record_id,)
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            self._parse_json_fields(d,
                dict_fields=("result_json",),
                list_fields=("participants", "tickers_discussed",
                             "final_ranking", "key_agreements",
                             "key_disagreements", "risk_warnings",
                             "transcript_json", "model_predictions"))
            return d
        finally:
            conn.close()


    def update_meeting_outcome(self, record_id: str, outcome_summary: str) -> bool:
        with self._write_lock:
            conn = self._connect()
            try:
                q = """UPDATE meeting_records SET outcome_recorded = 1,
                       outcome_summary = ? WHERE id = ?"""
                p = (outcome_summary, record_id)
                if self._user_id:
                    q += " AND user_id = ?"
                    p = p + (self._user_id,)
                cur = conn.execute(q, p)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()


    def update_meeting_review(self, record_id: str, *, transcript_json=None,
                              result_json=None, final_verdict: str | None = None) -> bool:
        """质询/复议后更新会议记录的 transcript / 共识 / 最终结论。仅更新传入的字段。"""
        sets: list[str] = []
        vals: list = []
        if transcript_json is not None:
            sets.append("transcript_json = ?")
            vals.append(json.dumps(transcript_json, ensure_ascii=False))
        if result_json is not None:
            sets.append("result_json = ?")
            vals.append(json.dumps(result_json, ensure_ascii=False))
        if final_verdict is not None:
            sets.append("final_verdict = ?")
            vals.append(final_verdict)
        if not sets:
            return False
        with self._write_lock:
            conn = self._connect()
            try:
                q = f"UPDATE meeting_records SET {', '.join(sets)} WHERE id = ?"
                p = tuple(vals) + (record_id,)
                if self._user_id:
                    q += " AND user_id = ?"
                    p = p + (self._user_id,)
                cur = conn.execute(q, p)
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()


    def get_meeting_stats(self, market: str = "") -> dict:
        conn = self._connect()
        try:
            q = "SELECT meeting_type, COUNT(*) as cnt FROM meeting_records"
            p: tuple = ()
            if market:
                q += " WHERE market = ?"
                p = (market,)
            q, p = self._user_filter(q, p)
            q += " GROUP BY meeting_type"
            rows = conn.execute(q, p).fetchall()
            by_type = {r["meeting_type"]: r["cnt"] for r in rows}

            q2 = "SELECT COUNT(*) as total, AVG(duration_seconds) as avg_duration FROM meeting_records"
            p2: tuple = ()
            if market:
                q2 += " WHERE market = ?"
                p2 = (market,)
            q2, p2 = self._user_filter(q2, p2)
            summary = dict(conn.execute(q2, p2).fetchone())
            return {
                "total": summary.get("total", 0),
                "avg_duration_seconds": round(summary.get("avg_duration") or 0, 1),
                "by_type": by_type,
            }
        finally:
            conn.close()


    def upsert_role_config(self, role_key: str, slot_index: int,
                           provider: str, model: str,
                           role_label: str = "", role_group: str = "",
                           user_id: str | None = None) -> str:
        uid = user_id if user_id is not None else (self._user_id or "")
        now = _now_iso()
        with self._write_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM ai_role_config WHERE role_key = ? AND slot_index = ? AND user_id = ?",
                (role_key, slot_index, uid),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE ai_role_config SET provider = ?, model = ?, is_active = 1,
                       updated_at = ?, role_label = COALESCE(NULLIF(?, ''), role_label),
                       role_group = COALESCE(NULLIF(?, ''), role_group)
                       WHERE id = ?""",
                    (provider, model, now, role_label, role_group, existing["id"]),
                )
                return existing["id"]
            rid = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO ai_role_config
                   (id, role_key, role_label, role_group, slot_index, provider, model,
                    is_active, created_at, updated_at, user_id)
                   VALUES (?,?,?,?,?,?,?,1,?,?,?)""",
                (rid, role_key, role_label, role_group, slot_index,
                 provider, model, now, now, uid),
            )
            return rid


    def get_role_configs(self, role_key: str | None = None,
                         role_group: str | None = None,
                         user_id: str | None = None) -> list[dict]:
        uid = user_id if user_id is not None else (self._user_id or "")
        conn = self._connect()
        try:
            q = "SELECT * FROM ai_role_config WHERE user_id = ? AND is_active = 1"
            p: tuple = (uid,)
            if role_key:
                q += " AND role_key = ?"
                p += (role_key,)
            if role_group:
                q += " AND role_group = ?"
                p += (role_group,)
            q += " ORDER BY role_key, slot_index"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def delete_role_config(self, role_key: str, slot_index: int,
                           user_id: str | None = None) -> bool:
        uid = user_id if user_id is not None else (self._user_id or "")
        with self._write_conn() as conn:
            cur = conn.execute(
                "DELETE FROM ai_role_config WHERE role_key = ? AND slot_index = ? AND user_id = ?",
                (role_key, slot_index, uid),
            )
            return cur.rowcount > 0


    def clear_role_configs(self, role_key: str, user_id: str | None = None) -> int:
        uid = user_id if user_id is not None else (self._user_id or "")
        with self._write_conn() as conn:
            cur = conn.execute(
                "DELETE FROM ai_role_config WHERE role_key = ? AND user_id = ?",
                (role_key, uid),
            )
            return cur.rowcount


    # ── provider_configs：内置/自定义 provider 的默认模型 + base_url + 显示名 覆盖（单一真源）──
    def upsert_provider_config(self, provider_id: str, default_model: str = "",
                               base_url: str = "", user_id: str | None = None,
                               display_name: str = "") -> str:
        uid = user_id if user_id is not None else (self._user_id or "")
        now = _now_iso()
        with self._write_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM provider_configs WHERE provider_id = ? AND user_id = ?",
                (provider_id, uid),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE provider_configs SET default_model = ?, base_url = ?, display_name = ?, updated_at = ? WHERE id = ?",
                    (default_model, base_url, display_name, now, existing["id"]),
                )
                return existing["id"]
            rid = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO provider_configs
                   (id, user_id, provider_id, default_model, base_url, display_name, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (rid, uid, provider_id, default_model, base_url, display_name, now),
            )
            return rid


    def get_provider_config(self, provider_id: str, user_id: str | None = None) -> dict | None:
        uid = user_id if user_id is not None else (self._user_id or "")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM provider_configs WHERE provider_id = ? AND user_id = ?",
                (provider_id, uid),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_provider_configs(self, user_id: str | None = None) -> list[dict]:
        uid = user_id if user_id is not None else (self._user_id or "")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM provider_configs WHERE user_id = ?", (uid,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def delete_provider_config(self, provider_id: str, user_id: str | None = None) -> bool:
        uid = user_id if user_id is not None else (self._user_id or "")
        with self._write_conn() as conn:
            cur = conn.execute(
                "DELETE FROM provider_configs WHERE provider_id = ? AND user_id = ?",
                (provider_id, uid),
            )
            return cur.rowcount > 0


    def save_test_result(self, provider: str, model: str, test_type: str,
                         score: float, raw_result: str = "{}",
                         user_id: str | None = None) -> str:
        uid = user_id if user_id is not None else (self._user_id or "")
        now = _now_iso()
        with self._write_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM model_capability_test WHERE provider = ? AND model = ? AND test_type = ? AND user_id = ?",
                (provider, model, test_type, uid),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE model_capability_test SET score = ?, raw_result = ?, tested_at = ? WHERE id = ?",
                    (score, raw_result, now, existing["id"]),
                )
                return existing["id"]
            rid = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO model_capability_test
                   (id, provider, model, test_type, score, raw_result, tested_at, user_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (rid, provider, model, test_type, score, raw_result, now, uid),
            )
            return rid


    def get_test_results(self, provider: str | None = None, model: str | None = None,
                         user_id: str | None = None) -> list[dict]:
        uid = user_id if user_id is not None else (self._user_id or "")
        conn = self._connect()
        try:
            q = "SELECT * FROM model_capability_test WHERE user_id = ?"
            p: tuple = (uid,)
            if provider:
                q += " AND provider = ?"
                p += (provider,)
            if model:
                q += " AND model = ?"
                p += (model,)
            q += " ORDER BY provider, model, test_type"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()


    def save_recommendation(self, role_key: str, slot_index: int,
                            provider: str, model: str,
                            composite_score: float = 0, score_breakdown: str = "{}",
                            reason: str = "", user_id: str | None = None) -> str:
        uid = user_id if user_id is not None else (self._user_id or "")
        now = _now_iso()
        with self._write_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM model_recommendation WHERE role_key = ? AND slot_index = ? AND user_id = ?",
                (role_key, slot_index, uid),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE model_recommendation SET recommended_provider = ?, recommended_model = ?,
                       composite_score = ?, score_breakdown = ?, reason = ?, generated_at = ?
                       WHERE id = ?""",
                    (provider, model, composite_score, score_breakdown, reason, now, existing["id"]),
                )
                return existing["id"]
            rid = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO model_recommendation
                   (id, role_key, slot_index, recommended_provider, recommended_model,
                    composite_score, score_breakdown, reason, generated_at, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rid, role_key, slot_index, provider, model,
                 composite_score, score_breakdown, reason, now, uid),
            )
            return rid


    def get_recommendations(self, role_key: str | None = None,
                            user_id: str | None = None) -> list[dict]:
        uid = user_id if user_id is not None else (self._user_id or "")
        conn = self._connect()
        try:
            q = "SELECT * FROM model_recommendation WHERE user_id = ?"
            p: tuple = (uid,)
            if role_key:
                q += " AND role_key = ?"
                p += (role_key,)
            q += " ORDER BY role_key, slot_index"
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()

