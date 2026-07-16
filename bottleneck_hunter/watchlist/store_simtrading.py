"""WatchlistStore mixin：模拟账户 / 持仓与成交 / 资金操作。"""

from __future__ import annotations

import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _SimTradingMixin:
    def get_sim_account(self) -> dict:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT * FROM sim_account LIMIT 1")
            row = conn.execute(q, p).fetchone()
            if row:
                return dict(row)
            market = self._market or "us_stock"
            if market == "a_stock":
                name, capital = "A股模拟账户", 1000000.0
            else:
                name, capital = "美股模拟账户", 100000.0
            aid = uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                f"""INSERT INTO sim_account
                   (id, name, initial_capital, current_capital, cash_balance,
                    total_equity, total_return_pct, total_trades, win_rate, created_at, updated_at{self._user_insert_cols()}{self._market_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()}{self._market_insert_vals()})""",
                (aid, name, capital, capital, capital,
                 capital, 0.0, 0, 0.0, now, now) + self._user_insert_params() + self._market_insert_params(),
            )
            conn.commit()
            return {"id": aid, "name": name, "initial_capital": capital,
                    "current_capital": capital, "cash_balance": capital,
                    "total_equity": capital, "total_return_pct": 0.0,
                    "total_trades": 0, "win_rate": 0.0, "created_at": now, "updated_at": now}
        finally:
            conn.close()


    def update_sim_account(self, **fields) -> bool:
        allowed = {"current_capital", "cash_balance", "total_equity", "total_return_pct",
                   "total_trades", "win_rate", "name", "initial_capital", "peak_equity"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        parts.append("updated_at = ?")
        vals.append(_now_iso())
        account = self.get_sim_account()
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

        get_sim_account() 在无账户时会自动 INSERT 默认账户，管理端查看不能触发副作用，
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


    def adjust_sim_funds(self, op_type: str, amount: float, note: str = "") -> dict:
        """增减资金，更新账户余额。"""
        account = self.get_sim_account()
        if not account:
            return {"error": "账户不存在"}
        if op_type == "withdraw" and account["cash_balance"] < amount:
            return {"error": "现金不足", "available": account["cash_balance"]}
        delta = amount if op_type == "deposit" else -amount
        new_cash = round(account["cash_balance"] + delta, 2)
        new_initial = round(account.get("initial_capital", 100000) + delta, 2)
        self.update_sim_account(cash_balance=new_cash, initial_capital=new_initial)
        self.create_fund_op(account["id"], op_type, amount, note)
        return {"success": True, "cash_balance": new_cash, "initial_capital": new_initial}

