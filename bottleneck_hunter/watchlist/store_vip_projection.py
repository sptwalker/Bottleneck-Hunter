"""WatchlistStore mixin：VIP 每日系统推算（待校准）+ 账户日志。

设计要点：
- 推算层 vip_projections 与月结单真值层 positions 物理隔离，只在读时叠加，绝不进入决策。
- 账户日志 vip_account_log 镜像 operation_log 模式，逐条记录推算/校准/异常/结算。
- 全部走 self._filtered / self._*_insert_* 助手，保证用户 + 市场隔离。
"""

from __future__ import annotations

import json
import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _VipProjectionMixin:
    # ── 推算：写入 ───────────────────────────────────────────────────
    def upsert_projection(
        self,
        *,
        account_ref: str,
        as_of_date: str,
        kind: str = "stock_mtm",
        ticker: str = "",
        quantity: float = 0.0,
        market_value_base: float = 0.0,
        unrealized_pnl: float = 0.0,
        basis: dict | None = None,
        status: str = "pending",
        confidence: float = 0.5,
    ) -> str:
        """写入/覆盖单条推算记录（按 UNIQUE 键 upsert），返回记录 id。"""
        account_ref = (account_ref or "").strip()
        now = _now_iso()
        basis_json = json.dumps(basis or {}, ensure_ascii=False)
        with self._write_conn() as conn:
            # 先查是否已存在同键记录（用户+市场+账户+日期+种类+标的）
            q, p = self._filtered(
                "SELECT id FROM vip_projections WHERE account_ref=? AND as_of_date=? AND kind=? AND ticker=? LIMIT 1",
                (account_ref, as_of_date, kind, ticker),
            )
            row = conn.execute(q, p).fetchone()
            if row:
                pid = row["id"]
                conn.execute(
                    """UPDATE vip_projections
                       SET quantity=?, market_value_base=?, unrealized_pnl=?, basis_json=?,
                           status=?, confidence=?, updated_at=?
                       WHERE id=?""",
                    (quantity, market_value_base, unrealized_pnl, basis_json,
                     status, confidence, now, pid),
                )
                return pid
            pid = uuid.uuid4().hex[:12]
            conn.execute(
                f"""INSERT INTO vip_projections
                   (id, account_ref, as_of_date, kind, ticker, quantity, market_value_base,
                    unrealized_pnl, basis_json, status, confidence, created_at, updated_at
                    {self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?
                    {self._user_insert_vals()}{self._market_insert_vals()})""",
                (pid, account_ref, as_of_date, kind, ticker, quantity, market_value_base,
                 unrealized_pnl, basis_json, status, confidence, now, now)
                + self._user_insert_params() + self._market_insert_params(),
            )
            return pid

    # ── 推算：读取 ───────────────────────────────────────────────────
    def list_projections(
        self,
        *,
        account_ref: str = "",
        as_of_date: str = "",
        since_date: str = "",
        until_date: str = "",
        kind: str = "",
        status: str = "",
    ) -> list[dict]:
        conn = self._connect()
        try:
            clauses, params = [], []
            if account_ref:
                clauses.append("account_ref=?"); params.append(account_ref)
            if as_of_date:
                clauses.append("as_of_date=?"); params.append(as_of_date)
            if since_date:
                clauses.append("as_of_date>=?"); params.append(since_date)
            if until_date:
                clauses.append("as_of_date<=?"); params.append(until_date)
            if kind:
                clauses.append("kind=?"); params.append(kind)
            if status:
                clauses.append("status=?"); params.append(status)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            q, p = self._filtered(
                f"SELECT * FROM vip_projections{where} ORDER BY as_of_date DESC, kind ASC, ticker ASC",
                tuple(params),
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()

    def latest_projection_date(self, account_ref: str = "") -> str:
        """返回该账户最近一次推算的 as_of_date（无则空串）。"""
        conn = self._connect()
        try:
            where = " WHERE account_ref=?" if account_ref else ""
            args = (account_ref,) if account_ref else ()
            q, p = self._filtered(
                f"SELECT MAX(as_of_date) AS d FROM vip_projections{where}", args
            )
            row = conn.execute(q, p).fetchone()
            return (row["d"] or "") if row else ""
        finally:
            conn.close()

    def latest_projection_map(self, account_ref: str = "") -> dict[str, dict]:
        """返回该账户最近一日、每个标的的推算记录（ticker→row），供读时叠加。"""
        d = self.latest_projection_date(account_ref)
        if not d:
            return {}
        rows = self.list_projections(account_ref=account_ref, as_of_date=d)
        return {r["ticker"]: r for r in rows}

    def mark_projection_calibrated(
        self, projection_id: str, *, doc_id: str = "", diff_pct: float | None = None, flagged: bool = False
    ) -> None:
        status = "flagged" if flagged else "calibrated"
        with self._write_conn() as conn:
            q, p = self._filtered(
                "UPDATE vip_projections SET status=?, calibrated_by_doc_id=?, calib_diff_pct=?, updated_at=? WHERE id=?",
                (status, doc_id, diff_pct, _now_iso(), projection_id),
            )
            conn.execute(q, p)

    # ── 账户日志 ─────────────────────────────────────────────────────
    def log_account_event(
        self,
        *,
        account_ref: str,
        event_type: str = "projection",
        title: str,
        detail: str = "",
        severity: str = "info",
        payload: dict | None = None,
    ) -> str:
        lid = uuid.uuid4().hex[:12]
        now = _now_iso()
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO vip_account_log
                   (id, account_ref, ts, event_type, title, detail, severity, payload_json
                    {self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?
                    {self._user_insert_vals()}{self._market_insert_vals()})""",
                (lid, (account_ref or "").strip(), now, event_type, title, detail, severity, payload_json)
                + self._user_insert_params() + self._market_insert_params(),
            )
        return lid

    def list_account_log(
        self, *, account_ref: str = "", event_type: str = "", limit: int = 200
    ) -> list[dict]:
        conn = self._connect()
        try:
            clauses, params = [], []
            if account_ref:
                clauses.append("account_ref=?"); params.append(account_ref)
            if event_type:
                clauses.append("event_type=?"); params.append(event_type)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            q, p = self._filtered(
                f"SELECT * FROM vip_account_log{where} ORDER BY ts DESC LIMIT ?",
                tuple(params) + (int(limit),),
            )
            return [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()
