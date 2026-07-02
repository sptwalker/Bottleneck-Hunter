"""WatchlistStore mixin：LLM 预算 / 预算配置 / 管道状态。"""

from __future__ import annotations

from datetime import datetime, timezone

from bottleneck_hunter.watchlist.store_base import _today


class _BudgetMixin:
    def record_llm_usage(self, usage: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO llm_budget
                   (date, provider, model, input_tokens, output_tokens, estimated_cost_usd, task_type{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    usage.get("date", _today()),
                    usage.get("provider", ""), usage.get("model", ""),
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    usage.get("estimated_cost_usd", 0.0), usage.get("task_type", ""),
                ) + self._user_insert_params(),
            )
            conn.commit()
        finally:
            conn.close()


    def get_daily_usage(self, date: str | None = None) -> dict:
        date = date or _today()
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date = ?""",
                (date,),
            )
            row = conn.execute(q, p).fetchone()
            return {"date": date, "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"], "cost": row["cost"]}
        finally:
            conn.close()


    def get_monthly_usage(self, year: int | None = None, month: int | None = None) -> dict:
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month
        prefix = f"{year}-{month:02d}"
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date LIKE ?""",
                (f"{prefix}%",),
            )
            row = conn.execute(q, p).fetchone()
            return {"year": year, "month": month, "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"], "cost": row["cost"]}
        finally:
            conn.close()


    def get_budget_limits(self) -> dict[str, float]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT key, value FROM budget_config")
            rows = conn.execute(q, p).fetchall()
            return {r["key"]: float(r["value"]) for r in rows}
        finally:
            conn.close()


    def set_budget_limit(self, key: str, value: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO budget_config(key, value{self._user_insert_cols()}) VALUES (?, ?{self._user_insert_vals()})",
                (key, str(value)) + self._user_insert_params(),
            )
            conn.commit()
        finally:
            conn.close()


    def update_pipeline_status(self, name: str, **fields) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR IGNORE INTO pipeline_status(pipeline_name{self._user_insert_cols()}) VALUES (?{self._user_insert_vals()})",
                (name,) + self._user_insert_params(),
            )
            allowed = {"last_run_at", "last_status", "last_error", "next_run_at",
                        "stocks_processed", "stocks_total"}
            parts, vals = [], []
            for k, v in fields.items():
                if k in allowed:
                    parts.append(f"{k} = ?")
                    vals.append(v)
            if parts:
                vals.append(name)
                q, p = self._user_filter(
                    f"UPDATE pipeline_status SET {', '.join(parts)} WHERE pipeline_name = ?", tuple(vals)
                )
                conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()


    def get_pipeline_statuses(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM pipeline_status")
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_stale_tickers(self, max_age_hours: int = 48) -> list[dict]:
        """返回快照数据超过 max_age_hours 的活跃 ticker 列表。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """
                SELECT w.ticker, w.company_name, w.market,
                       MAX(ms.date) AS last_date
                FROM watchlist w
                LEFT JOIN market_snapshots ms ON w.ticker = ms.ticker
                WHERE w.is_active = 1
                GROUP BY w.ticker
                HAVING last_date IS NULL
                   OR last_date < date('now', ?)
                """,
                (f"-{max_age_hours} hours",),
                table="w",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

