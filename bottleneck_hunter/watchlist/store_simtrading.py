"""WatchlistStore mixin：模拟账户 / 持仓与成交 / 资金操作。"""

from __future__ import annotations

import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _SimTradingMixin:
    def _next_vip_account_sort_order(self) -> int:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT MAX(sort_order) AS max_sort FROM vip_accounts WHERE account_ref != ''")
            row = conn.execute(q, p).fetchone()
            return int((row["max_sort"] if row and row["max_sort"] is not None else -1) + 1)
        finally:
            conn.close()

    def list_vip_accounts(self, *, include_hidden_default: bool = True) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM vip_accounts ORDER BY CASE WHEN account_ref = '' THEN 1 ELSE 0 END, sort_order ASC, created_at ASC"
            )
            rows = [dict(r) for r in conn.execute(q, p).fetchall()]
            if include_hidden_default:
                return rows
            return [row for row in rows if (row.get("account_ref") or "").strip()]
        finally:
            conn.close()


    def resolve_vip_account_ref(self, account_ref: str = "") -> str:
        account_ref = (account_ref or "").strip()
        if account_ref:
            return account_ref
        refs = [row.get("account_ref", "") for row in self.list_vip_accounts(include_hidden_default=False)]
        if len(refs) == 1:
            return refs[0]
        if not refs:
            raise ValueError("未提供 account_ref，且当前没有可用账户")
        raise ValueError("未提供 account_ref，且存在多个账户，请明确指定 account_ref")


    def ensure_vip_account(
        self,
        account_ref: str = "",
        *,
        display_name: str = "",
        institution_name: str = "",
        account_kind: str = "broker",
        is_default: bool = False,
    ) -> dict:
        account_ref = (account_ref or "").strip()
        if not account_ref:
            raise ValueError("account_ref 不能为空")
        display_name = (display_name or "").strip()
        institution_name = (institution_name or "").strip()
        account_kind = account_kind if account_kind in {"bank", "broker"} else "broker"
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM vip_accounts WHERE account_ref=? LIMIT 1", (account_ref,))
            row = conn.execute(q, p).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
        if not display_name:
            display_name = account_ref
        aid = uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._write_conn() as conn:
            if is_default:
                q, p = self._filtered("UPDATE vip_accounts SET is_default=0")
                conn.execute(q, p)
            sort_order = self._next_vip_account_sort_order()
            conn.execute(
                f"""INSERT INTO vip_accounts
                   (id, account_ref, display_name, institution_name, account_kind, is_default, sort_order,
                    created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (aid, account_ref, display_name, institution_name, account_kind, 1 if is_default else 0, sort_order, now, now)
                + self._user_insert_params() + self._market_insert_params(),
            )
        return {
            "id": aid,
            "account_ref": account_ref,
            "display_name": display_name,
            "institution_name": institution_name,
            "account_kind": account_kind,
            "is_default": 1 if is_default else 0,
            "sort_order": sort_order,
            "created_at": now,
            "updated_at": now,
        }


    def create_vip_account(
        self,
        *,
        account_ref: str,
        display_name: str = "",
        institution_name: str = "",
        account_kind: str = "broker",
        is_default: bool = False,
    ) -> dict:
        account_ref = (account_ref or "").strip()
        if not account_ref:
            raise ValueError("account_ref 不能为空")
        return self.ensure_vip_account(
            account_ref,
            display_name=display_name,
            institution_name=institution_name,
            account_kind=account_kind,
            is_default=is_default,
        )


    def get_default_vip_account(self) -> dict:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM vip_accounts WHERE is_default=1 AND account_ref != '' ORDER BY created_at ASC LIMIT 1"
            )
            row = conn.execute(q, p).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
        accounts = self.list_vip_accounts(include_hidden_default=False)
        if len(accounts) == 1:
            return accounts[0]
        if not accounts:
            raise ValueError("当前没有默认账户，请先创建账户")
        raise ValueError("当前没有默认账户，请先设置默认账户")


    def set_default_vip_account(self, account_ref: str = "") -> dict:
        self.ensure_vip_account(account_ref)
        with self._write_conn() as conn:
            q, p = self._filtered("UPDATE vip_accounts SET is_default=0")
            conn.execute(q, p)
            q, p = self._filtered("UPDATE vip_accounts SET is_default=1, updated_at=? WHERE account_ref=?", (_now_iso(), account_ref))
            conn.execute(q, p)
        return self.ensure_vip_account(account_ref)


    def update_vip_account(self, account_ref: str = "", **fields) -> dict:
        """更新账户元数据白名单字段（display_name/institution_name/account_kind/is_default）。account_ref 不可改。"""
        account_ref = (account_ref or "").strip()
        self.ensure_vip_account(account_ref)
        allowed = {"display_name", "institution_name", "account_kind"}
        parts, vals = [], []
        for k, v in fields.items():
            if k == "account_kind" and v not in {"bank", "broker"}:
                continue
            if k in allowed and v is not None:
                parts.append(f"{k} = ?")
                vals.append(str(v).strip())
        if parts:
            parts.append("updated_at = ?")
            vals.append(_now_iso())
            with self._write_conn() as conn:
                q, p = self._filtered(
                    f"UPDATE vip_accounts SET {', '.join(parts)} WHERE account_ref = ?",
                    tuple(vals) + (account_ref,),
                )
                conn.execute(q, p)
        if fields.get("is_default"):
            return self.set_default_vip_account(account_ref)
        return self.ensure_vip_account(account_ref)


    def vip_account_reference_counts(self, account_ref: str = "") -> dict:
        """统计账户下仍被引用的数据量（持仓/交易/导入/报告/衍生品/会话），用于删除守卫。"""
        account_ref = (account_ref or "").strip()
        conn = self._connect()
        try:
            counts = {}
            for table in ("transactions", "vip_imports", "vip_reports", "vip_derivative_terms", "chat_sessions", "chat_messages"):
                q, p = self._filtered(f"SELECT COUNT(*) AS n FROM {table} WHERE account_ref = ?", (account_ref,))
                counts[table] = conn.execute(q, p).fetchone()["n"]
            # sim_* 记录按 sim_account.id 关联，需先取该 ref 的 sim 槽
            q, p = self._filtered("SELECT id FROM sim_account WHERE account_ref = ? LIMIT 1", (account_ref,))
            row = conn.execute(q, p).fetchone()
            if row:
                account_id = row["id"]
                q, p = self._filtered(
                    "SELECT COUNT(*) AS n FROM sim_positions WHERE account_id = ? AND shares > 0", (account_id,)
                )
                counts["sim_positions"] = conn.execute(q, p).fetchone()["n"]
                q, p = self._filtered("SELECT COUNT(*) AS n FROM sim_trades WHERE account_id = ?", (account_id,))
                counts["sim_trades"] = conn.execute(q, p).fetchone()["n"]
                q, p = self._filtered("SELECT COUNT(*) AS n FROM sim_fund_ops WHERE account_id = ?", (account_id,))
                counts["sim_fund_ops"] = conn.execute(q, p).fetchone()["n"]
            else:
                counts["sim_positions"] = 0
                counts["sim_trades"] = 0
                counts["sim_fund_ops"] = 0
            return counts
        finally:
            conn.close()


    def delete_vip_account_data(self, account_ref: str = "") -> dict:
        """清理账户下旧数据；不删除账户本身。"""
        account_ref = (account_ref or "").strip()
        self.ensure_vip_account(account_ref)
        refs = self.vip_account_reference_counts(account_ref)
        with self._write_conn() as conn:
            q, p = self._filtered("SELECT id FROM sim_account WHERE account_ref = ? LIMIT 1", (account_ref,))
            row = conn.execute(q, p).fetchone()
            if row:
                account_id = row["id"]
                q, p = self._filtered("DELETE FROM sim_positions WHERE account_id = ?", (account_id,))
                conn.execute(q, p)
                q, p = self._filtered("DELETE FROM sim_trades WHERE account_id = ?", (account_id,))
                conn.execute(q, p)
                q, p = self._filtered("DELETE FROM sim_fund_ops WHERE account_id = ?", (account_id,))
                conn.execute(q, p)
                q, p = self._filtered("DELETE FROM sim_account WHERE id = ?", (account_id,))
                conn.execute(q, p)
            for table in ("transactions", "vip_imports", "vip_reports", "vip_derivative_terms", "chat_messages", "chat_sessions"):
                q, p = self._filtered(f"DELETE FROM {table} WHERE account_ref = ?", (account_ref,))
                conn.execute(q, p)
        return {"cleared": account_ref, "reference_counts": refs}


    def delete_vip_account(self, account_ref: str = "") -> dict:
        """删除空账户；仍有引用数据时抛 ValueError。"""
        account_ref = (account_ref or "").strip()
        account = self.ensure_vip_account(account_ref)
        if account.get("is_default"):
            raise ValueError("默认账户不可删除，请先切换默认账户")
        refs = self.vip_account_reference_counts(account_ref)
        used = {k: v for k, v in refs.items() if v}
        if used:
            raise ValueError(f"账户下仍有数据，不能删除：{used}")
        with self._write_conn() as conn:
            # 空 sim 槽随账户一并清掉，避免残留孤儿槽
            q, p = self._filtered("DELETE FROM sim_account WHERE account_ref = ?", (account_ref,))
            conn.execute(q, p)
            q, p = self._filtered("DELETE FROM vip_accounts WHERE account_ref = ?", (account_ref,))
            conn.execute(q, p)
        return {"deleted": account_ref}


    def set_vip_account_order(self, account_refs: list[str]) -> list[dict]:
        refs = [(ref or "").strip() for ref in account_refs if (ref or "").strip()]
        if not refs:
            return self.list_vip_accounts(include_hidden_default=False)
        existing = self.list_vip_accounts(include_hidden_default=False)
        existing_refs = [row.get("account_ref", "") for row in existing]
        if set(refs) != set(existing_refs):
            raise ValueError("账户排序列表不完整或包含无效账户")
        now = _now_iso()
        with self._write_conn() as conn:
            for idx, ref in enumerate(refs):
                q, p = self._filtered(
                    "UPDATE vip_accounts SET sort_order = ?, updated_at = ? WHERE account_ref = ?",
                    (idx, now, ref),
                )
                conn.execute(q, p)
        return self.list_vip_accounts(include_hidden_default=False)


    def get_sim_account(self, account_ref: str = "") -> dict:
        account_ref = (account_ref or "").strip()
        if account_ref:
            # VIP 子账户：显式真实账户，需经账户解析 + 建账
            target_ref = self.resolve_vip_account_ref(account_ref)
            default_name = (self.ensure_vip_account(target_ref).get("display_name") or target_ref).strip()
        else:
            # 决策中心自有模拟组合：单一 sim_account(account_ref='')，与 VIP 多账户彻底解绑。
            # 绝不经 resolve_vip_account_ref——否则用户有多个 VIP 账户时会误抛错，拖垮整个决策中心。
            target_ref, default_name = "", ""
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM sim_account WHERE account_ref = ? LIMIT 1", (target_ref,))
            row = conn.execute(q, p).fetchone()
            if row:
                return dict(row)
            market = self._market or "us_stock"
            if target_ref:
                # VIP 真实券商账户：初始权益为 0，只由结算单导入(materialize)填充，
                # 绝不预置模拟本金——否则未导入就凭空显示 10 万/100 万权益。
                name, capital = default_name or target_ref, 0.0
            else:
                # 决策中心自有模拟组合：预置模拟本金（美股 100 万美元 / A股 500 万人民币）。
                if market == "a_stock":
                    name, capital = "A股模拟账户", 5000000.0
                else:
                    name, capital = "美股模拟账户", 1000000.0
                name = (default_name or name).strip()
            aid = uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                f"""INSERT INTO sim_account
                   (id, name, initial_capital, current_capital, cash_balance,
                    total_equity, total_return_pct, total_trades, win_rate, account_ref, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (aid, name, capital, capital, capital,
                 capital, 0.0, 0, 0.0, target_ref, now, now) + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return {"id": aid, "name": name, "initial_capital": capital,
                    "current_capital": capital, "cash_balance": capital,
                    "total_equity": capital, "total_return_pct": 0.0,
                    "total_trades": 0, "win_rate": 0.0, "account_ref": target_ref,
                    "created_at": now, "updated_at": now}
        finally:
            conn.close()


    def update_sim_account(self, account_ref: str = "", **fields) -> bool:
        allowed = {"current_capital", "cash_balance", "total_equity", "total_return_pct",
                   "total_trades", "win_rate", "name", "initial_capital", "peak_equity",
                   "loan_balance"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        parts.append("updated_at = ?")
        vals.append(_now_iso())
        account = self.get_sim_account(account_ref=account_ref)
        vals.append(account["id"])
        with self._write_conn() as conn:
            q, p = self._filtered(
                f"UPDATE sim_account SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def get_sim_positions(self, account_id: str | None = None, include_zero: bool = False) -> list[dict]:
        conn = self._connect()
        try:
            share_filter = "" if include_zero else " AND shares > 0"
            if account_id:
                q, p = self._filtered(
                    f"SELECT * FROM sim_positions WHERE account_id = ?{share_filter}", (account_id,)
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(f"SELECT * FROM sim_positions WHERE 1=1{share_filter}")
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def list_sim_accounts(self, user_id: str) -> list[dict]:
        """只读列出某用户所有市场的模拟账户（不自动创建）——供管理端聚合查看。

        get_sim_account() 只接受真实账户，管理端查看不能触发副作用，
        故此处直接按 user_id 读全部市场的账户行。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sim_account WHERE user_id = ? ORDER BY market",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def create_sim_trade(self, account_id: str, ticker: str, side: str,
                         shares: int, price: float, amount: float,
                         execution_plan_id: str | None = None,
                         entry_id: str | None = None,
                         trade_type: str = "entry", reasoning: str = "",
                         slippage_bps: float = 0.0,
                         realized_pnl: float | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO sim_trades
                   (id, account_id, execution_plan_id, entry_id, ticker, side,
                    shares, price, amount, trade_type, reasoning, slippage_bps, realized_pnl, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (tid, account_id, execution_plan_id, entry_id, ticker, side,
                 shares, price, amount, trade_type, reasoning, slippage_bps, realized_pnl, _now_iso()) + self._user_insert_params() + self._market_insert_params(),
            )
            return tid


    def get_sim_position(self, account_id: str, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM sim_positions WHERE account_id = ? AND ticker = ? AND shares > 0",
                (account_id, ticker),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_sim_position_any(self, account_id: str, ticker: str) -> dict | None:
        """查找持仓记录（含 shares=0），用于买回复用已有记录。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM sim_positions WHERE account_id = ? AND ticker = ?",
                (account_id, ticker),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def delete_sim_position_if_zero(self, position_id: str) -> bool:
        """仅允许删除 shares=0 的持仓记录。"""
        with self._write_conn() as conn:
            q, p = self._filtered(
                "DELETE FROM sim_positions WHERE id = ? AND shares = 0", (position_id,)
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def create_sim_position(self, account_id: str, ticker: str,
                            shares: int, avg_cost: float,
                            entry_id: str | None = None) -> str:
        from bottleneck_hunter.watchlist.store_base import normalize_ticker
        ticker = normalize_ticker(ticker, self._market)  # 归一：持仓 ticker 与执行计划/观察池对齐
        pid = uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO sim_positions
                   (id, account_id, entry_id, ticker, shares, avg_cost,
                    current_price, market_value, unrealized_pnl, weight_pct,
                    opened_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (pid, account_id, entry_id, ticker, shares, avg_cost,
                 avg_cost, shares * avg_cost, 0.0, 0.0, now, now) + self._user_insert_params() + self._market_insert_params(),
            )
            return pid


    def update_sim_position(self, position_id: str, **fields) -> bool:
        allowed = {"shares", "avg_cost", "current_price", "market_value",
                   "unrealized_pnl", "weight_pct", "entry_id"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        parts.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(position_id)
        with self._write_conn() as conn:
            q, p = self._filtered(
                f"UPDATE sim_positions SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def delete_sim_position(self, position_id: str) -> bool:
        with self._write_conn() as conn:
            q, p = self._filtered("DELETE FROM sim_positions WHERE id = ?", (position_id,))
            cur = conn.execute(q, p)
            return cur.rowcount > 0


    def get_sim_trades(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._filtered(
                    "SELECT * FROM sim_trades WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(
                    "SELECT * FROM sim_trades ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def create_fund_op(self, account_id: str, op_type: str, amount: float,
                       note: str = "") -> str:
        fid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO sim_fund_ops
                   (id, account_id, op_type, amount, note, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (fid, account_id, op_type, amount, note, _now_iso()) + self._user_insert_params() + self._market_insert_params(),
            )
            return fid


    def get_fund_ops(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT * FROM sim_fund_ops ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def adjust_sim_funds(self, op_type: str, amount: float, note: str = "", account_ref: str = "") -> dict:
        """增减资金，更新账户余额。"""
        account = self.get_sim_account(account_ref=account_ref)
        if not account:
            return {"error": "账户不存在"}
        if op_type == "withdraw" and account["cash_balance"] < amount:
            return {"error": "现金不足", "available": account["cash_balance"]}
        delta = amount if op_type == "deposit" else -amount
        new_cash = round(account["cash_balance"] + delta, 2)
        new_initial = round(account.get("initial_capital", 100000) + delta, 2)
        self.update_sim_account(account_ref=account_ref, cash_balance=new_cash, initial_capital=new_initial)
        self.create_fund_op(account["id"], op_type, amount, note)
        return {"success": True, "cash_balance": new_cash, "initial_capital": new_initial}


    # ── VIP 导入历史 ─────────────────────────────────────────────────────
    def create_vip_import(self, *, file_name: str, file_hash: str, file_type: str,
                          detected_kind: str, status: str, summary: str = "",
                          key_metrics: dict | None = None, reason: str = "", account_ref: str = "") -> str:
        """写一条导入历史。同 (user, market, account_ref, file_hash) 已存在则更新为最新结果（upsert）。"""
        import json
        account_ref = self.resolve_vip_account_ref(account_ref)
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT id FROM vip_imports WHERE account_ref=? AND file_hash = ?",
                (account_ref, file_hash),
            )
            row = conn.execute(q, p).fetchone()
        finally:
            conn.close()
        if row:
            # 修好解析后重导：旧行可能停在 rejected/unparseable，须刷新为最新结果，否则历史长期误导
            with self._write_conn() as conn:
                conn.execute(
                    "UPDATE vip_imports SET file_name=?, file_type=?, detected_kind=?, status=?, "
                    "summary=?, key_metrics_json=?, reason=?, created_at=? WHERE id=?",
                    (file_name, file_type, detected_kind, status, summary,
                     json.dumps(key_metrics or {}, ensure_ascii=False), reason, _now_iso(), row["id"]),
                )
            return row["id"]
        iid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO vip_imports
                   (id, file_name, file_hash, file_type, detected_kind, status, summary,
                    key_metrics_json, reason, account_ref, created_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (iid, file_name, file_hash, file_type, detected_kind, status, summary,
                 json.dumps(key_metrics or {}, ensure_ascii=False), reason, account_ref, _now_iso())
                + self._user_insert_params() + self._market_insert_params(),
            )
            return iid


    def list_vip_imports(self, limit: int = 100, account_ref: str = "", scope: str = "account") -> list[dict]:
        import json
        account_ref = (account_ref or "").strip()
        conn = self._connect()
        try:
            if scope == "all":
                q, p = self._filtered(
                    "SELECT * FROM vip_imports ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                q, p = self._filtered(
                    "SELECT * FROM vip_imports WHERE account_ref=? ORDER BY created_at DESC LIMIT ?",
                    (self.resolve_vip_account_ref(account_ref), limit),
                )
            rows = [dict(r) for r in conn.execute(q, p).fetchall()]
        finally:
            conn.close()
        for r in rows:
            r["key_metrics"] = json.loads(r.pop("key_metrics_json", "") or "{}")
        return rows


    def count_vip_imports_by_account(self) -> dict[str, dict]:
        """各账户累计导入文件数 + 按类型(detected_kind)细分。一次聚合避免 N+1。
        返回 {account_ref: {"total": n, "by_type": {kind: n}}}。"""
        conn = self._connect()
        try:
            q, p = self._filtered(
                "SELECT COALESCE(account_ref,'') ref, COALESCE(detected_kind, file_type, '未知') kind, COUNT(*) n "
                "FROM vip_imports GROUP BY ref, kind"
            )
            rows = conn.execute(q, p).fetchall()
        finally:
            conn.close()
        out: dict[str, dict] = {}
        for r in rows:
            slot = out.setdefault(r["ref"], {"total": 0, "by_type": {}})
            slot["total"] += r["n"]
            slot["by_type"][r["kind"]] = slot["by_type"].get(r["kind"], 0) + r["n"]
        return out


    # ── 转发银行邮件解读管道（管理员专用）──────────────────────────────────
    # 说明：解读记录/待确认队列跨多个 VIP 用户，管理员端需汇总查看 → 读用「全局无过滤」查询；
    #      写走本 store 的用户/市场 scope（poll 时对每封邮件 .for_user(vip) 后调用）。
    def find_mail_log_by_msgid(self, msgid: str) -> dict | None:
        """邮件级去重：msgid 全局 UNIQUE，跨用户查一行。"""
        if not (msgid or "").strip():
            return None
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM mail_ingest_log WHERE msgid=?", (msgid,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_mail_ingest_log(self, *, msgid: str, sender: str, summary: str = "", n_body_txn: int = 0,
                               attachments: list | None = None, status: str = "processed", reason: str = "") -> str:
        import json
        iid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO mail_ingest_log
                   (id, msgid, sender, summary, n_body_txn, attachments_json, status, reason, created_at
                    {self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (iid, msgid, sender, summary, int(n_body_txn),
                 json.dumps(attachments or [], ensure_ascii=False), status, reason, _now_iso())
                + self._user_insert_params() + self._market_insert_params(),
            )
        return iid

    def list_mail_ingest_log(self, limit: int = 100) -> list[dict]:
        """管理员端全局汇总（跨 VIP 用户），最新在前。"""
        import json
        conn = self._connect()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM mail_ingest_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]
        finally:
            conn.close()
        for r in rows:
            r["attachments"] = json.loads(r.pop("attachments_json", "") or "[]")
        return rows

    def create_pending_txn(self, *, msgid: str, account_ref: str, txn: dict) -> str:
        import json
        iid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO vip_mail_confirm_pending
                   (id, msgid, account_ref, txn_json, status, created_at
                    {self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (iid, msgid, account_ref, json.dumps(txn, ensure_ascii=False), "pending", _now_iso())
                + self._user_insert_params() + self._market_insert_params(),
            )
        return iid

    def list_pending_txns(self, status: str = "pending", limit: int = 200) -> list[dict]:
        """管理员端全局汇总待确认交易（跨 VIP 用户）。status='' 表示全部状态。"""
        import json
        conn = self._connect()
        try:
            if status:
                sql = "SELECT * FROM vip_mail_confirm_pending WHERE status=? ORDER BY created_at DESC LIMIT ?"
                rows = [dict(r) for r in conn.execute(sql, (status, limit)).fetchall()]
            else:
                sql = "SELECT * FROM vip_mail_confirm_pending ORDER BY created_at DESC LIMIT ?"
                rows = [dict(r) for r in conn.execute(sql, (limit,)).fetchall()]
        finally:
            conn.close()
        for r in rows:
            r["txn"] = json.loads(r.pop("txn_json", "") or "{}")
        return rows

    def get_pending_txn(self, pending_id: str) -> dict | None:
        import json
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM vip_mail_confirm_pending WHERE id=?", (pending_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        d = dict(row)
        d["txn"] = json.loads(d.pop("txn_json", "") or "{}")
        return d

    def mark_pending(self, pending_id: str, status: str) -> None:
        with self._write_conn() as conn:
            conn.execute("UPDATE vip_mail_confirm_pending SET status=? WHERE id=?", (status, pending_id))


