"""WatchlistStore mixin：UZI 分析 / 情报记录 / 策略记录。"""

from __future__ import annotations

import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _IntelMixin:
    def create_uzi_analysis(self, entry_id: str, ticker: str,
                            analysis_type: str) -> str:
        analysis_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO uzi_analyses
                   (id, entry_id, ticker, analysis_type, status, started_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (analysis_id, entry_id, ticker, analysis_type, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return analysis_id
        finally:
            conn.close()


    def complete_uzi_analysis(self, analysis_id: str, result_json: str,
                               summary: str = "", score: float | None = None,
                               signal: str | None = None,
                               trap_level: str | None = None) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE uzi_analyses SET
                   status='completed', completed_at=?, result_json=?,
                   summary=?, score=?, signal=?, trap_level=?
                   WHERE id=?""",
                (_now_iso(), result_json, summary, score, signal,
                 trap_level, analysis_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def fail_uzi_analysis(self, analysis_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE uzi_analyses SET
                   status='failed', completed_at=?, summary=?
                   WHERE id=?""",
                (_now_iso(), error, analysis_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def get_uzi_history(self, entry_id: str,
                        limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, analysis_type, status,
                   started_at, completed_at, summary, score, signal, trap_level
                   FROM uzi_analyses WHERE entry_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_uzi_analysis(self, analysis_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM uzi_analyses WHERE id = ?", (analysis_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def create_intelligence(self, entry_id: str, ticker: str) -> tuple[str, int]:
        """Create a new intelligence record. Returns (intelligence_id, version)."""
        intel_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM stock_intelligence WHERE entry_id = ?",
                (entry_id,)
            )
            version = conn.execute(q, p).fetchone()[0]
            conn.execute(
                f"""INSERT INTO stock_intelligence
                   (id, entry_id, ticker, version, status, created_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (intel_id, entry_id, ticker, version, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return intel_id, version
        finally:
            conn.close()


    def complete_intelligence(self, intel_id: str, *,
                              price_summary: str = "{}",
                              news_summary: str = "{}",
                              sec_summary: str = "{}",
                              options_summary: str = "{}",
                              earnings_summary: str = "{}",
                              source_scorecard_summary: str = "{}",
                              brief_text: str = "",
                              key_signals: str = "[]",
                              data_freshness: str = "{}") -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE stock_intelligence SET
                   status='completed', completed_at=?,
                   price_summary=?, news_summary=?, sec_summary=?,
                   options_summary=?, earnings_summary=?, source_scorecard_summary=?,
                   brief_text=?, key_signals=?, data_freshness=?
                   WHERE id=?""",
                (_now_iso(), price_summary, news_summary, sec_summary,
                 options_summary, earnings_summary, source_scorecard_summary,
                 brief_text, key_signals, data_freshness, intel_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def fail_intelligence(self, intel_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE stock_intelligence SET
                   status='failed', completed_at=?, error=?
                   WHERE id=?""",
                (_now_iso(), error, intel_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def get_latest_intelligence(self, entry_id: str) -> dict | None:
        """Returns the most recent completed intelligence record."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT * FROM stock_intelligence
                   WHERE entry_id = ? AND status = 'completed'
                   ORDER BY version DESC LIMIT 1""",
                (entry_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_intelligence_history(self, entry_id: str, limit: int = 10) -> list[dict]:
        """Returns intelligence history (metadata only, no full JSON bodies)."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, version, status,
                   created_at, completed_at, error, data_freshness
                   FROM stock_intelligence WHERE entry_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_intelligence(self, intel_id: str) -> dict | None:
        """Returns a single intelligence record with all JSON fields."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM stock_intelligence WHERE id = ?", (intel_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def create_strategy(self, entry_id: str, ticker: str,
                        intelligence_id: str | None = None) -> tuple[str, int]:
        """Create a new strategy record. Returns (strategy_id, version)."""
        strategy_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_records WHERE entry_id = ?",
                (entry_id,)
            )
            version = conn.execute(q, p).fetchone()[0]
            conn.execute(
                f"""INSERT INTO strategy_records
                   (id, entry_id, ticker, intelligence_id, version, status, created_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (strategy_id, entry_id, ticker, intelligence_id, version, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return strategy_id, version
        finally:
            conn.close()


    def complete_strategy(self, strategy_id: str, *,
                          intelligence_summary: str = "",
                          bull_bear_analysis: str = "{}",
                          core_logic: str = "",
                          action_strategy: str = "{}",
                          risk_control: str = "{}",
                          targets_timeline: str = "{}",
                          strategy_comparison: str = "{}",
                          confidence_rating: str = "{}",
                          signal: str = "neutral",
                          confidence: int = 5,
                          reasoning_chain: str = "") -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE strategy_records SET
                   status='completed', completed_at=?,
                   intelligence_summary=?, bull_bear_analysis=?, core_logic=?,
                   action_strategy=?, risk_control=?, targets_timeline=?,
                   strategy_comparison=?, confidence_rating=?,
                   signal=?, confidence=?, reasoning_chain=?
                   WHERE id=?""",
                (_now_iso(), intelligence_summary, bull_bear_analysis, core_logic,
                 action_strategy, risk_control, targets_timeline,
                 strategy_comparison, confidence_rating,
                 signal, confidence, reasoning_chain, strategy_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def fail_strategy(self, strategy_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE strategy_records SET
                   status='failed', completed_at=?, error=?
                   WHERE id=?""",
                (_now_iso(), error, strategy_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def get_latest_strategy(self, entry_id: str) -> dict | None:
        """Returns the most recent completed strategy record."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT * FROM strategy_records
                   WHERE entry_id = ? AND status = 'completed'
                   ORDER BY version DESC LIMIT 1""",
                (entry_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_strategy_history(self, entry_id: str, limit: int = 10) -> list[dict]:
        """Returns strategy history (metadata only)."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, intelligence_id, version,
                   signal, confidence, status, created_at, completed_at, error
                   FROM strategy_records WHERE entry_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_strategy(self, strategy_id: str) -> dict | None:
        """Returns a single strategy record with all fields."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM strategy_records WHERE id = ?", (strategy_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_previous_strategy(self, entry_id: str,
                              exclude_id: str | None = None) -> dict | None:
        """Returns the most recent completed strategy BEFORE the current one."""
        conn = self._connect()
        try:
            if exclude_id:
                q, p = self._user_filter(
                    """SELECT * FROM strategy_records
                       WHERE entry_id = ? AND status = 'completed' AND id != ?
                       ORDER BY version DESC LIMIT 1""",
                    (entry_id, exclude_id)
                )
                row = conn.execute(q, p).fetchone()
            else:
                # Get second-most-recent
                q, p = self._user_filter(
                    """SELECT * FROM strategy_records
                       WHERE entry_id = ? AND status = 'completed'
                       ORDER BY version DESC LIMIT 1 OFFSET 1""",
                    (entry_id,)
                )
                row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_all_strategy_summaries(self) -> dict[str, dict]:
        """Returns {entry_id: {signal, confidence, version, created_at}} for all entries."""
        conn = self._connect()
        try:
            # 子查询无法被 _user_filter 的字符串插入安全处理（会误插到内层 GROUP BY 前），
            # 故此处显式对内/外两层 WHERE 都做 user_id 过滤，正确且安全。
            uid = getattr(self, "_user_id", "")
            cond = " AND user_id = ?" if uid else ""
            params = (uid, uid) if uid else ()
            q = f"""SELECT entry_id, signal, confidence, version, created_at
                   FROM strategy_records
                   WHERE status = 'completed'{cond}
                   AND (entry_id, version) IN (
                       SELECT entry_id, MAX(version)
                       FROM strategy_records
                       WHERE status = 'completed'{cond}
                       GROUP BY entry_id
                   )"""
            rows = conn.execute(q, params).fetchall()
            return {
                r["entry_id"]: {
                    "signal": r["signal"],
                    "confidence": r["confidence"],
                    "version": r["version"],
                    "created_at": r["created_at"],
                }
                for r in rows
            }
        finally:
            conn.close()

