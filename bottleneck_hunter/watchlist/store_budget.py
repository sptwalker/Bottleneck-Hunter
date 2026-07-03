"""WatchlistStore mixin：LLM 预算 / 预算配置 / 管道状态 / 自动更新配置。"""

from __future__ import annotations

from datetime import datetime, timezone

from bottleneck_hunter.watchlist.store_base import _today


# 自动更新配置默认值（用户未显式设置时的回退）。总开关 + 各分类开关默认全开，陈旧阈值 24h。
AUTO_UPDATE_DEFAULTS: dict[str, str] = {
    "master_enabled": "1",
    "watchlist_data": "1",    # 行情/新闻/公告/期权/机构等数据管道
    "daily_decision": "1",    # L1-L4 + 投委会日常决策
    "weekly_strategy": "1",   # 周度 L1/L2 策略重生成
    "auto_review": "1",       # 卖出复盘 + 机会成本 + 偏好学习
    "catalyst": "1",          # 催化剂扫描与判定
    "full_refresh": "1",      # 周期性全量刷新（数据+决策一条龙）
    "stale_threshold_hours": "24",
}


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


    # ── 自动更新配置（per-user，复合主键 key+user_id） ──────────────
    def get_auto_update_config(self) -> dict[str, str]:
        """返回当前用户的自动更新配置（合并默认值）。"""
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT key, value FROM auto_update_config")
            rows = conn.execute(q, p).fetchall()
            cfg = dict(AUTO_UPDATE_DEFAULTS)
            for r in rows:
                if r["key"] in cfg:
                    cfg[r["key"]] = r["value"]
            return cfg
        finally:
            conn.close()

    def set_auto_update_config(self, key: str, value: str) -> None:
        """写入单个自动更新配置项（复合主键 key+user_id，per-user 隔离正确）。"""
        if key not in AUTO_UPDATE_DEFAULTS:
            return  # 只接受已知 key，避免脏数据
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO auto_update_config(key, value, user_id) VALUES (?, ?, ?)",
                (key, str(value), getattr(self, "_user_id", "") or ""),
            )
            conn.commit()
        finally:
            conn.close()

    def is_auto_update_enabled(self, category: str) -> bool:
        """当前用户是否启用某分类的自动更新（总开关 + 分类开关都为 '1' 才算开）。"""
        cfg = self.get_auto_update_config()
        if cfg.get("master_enabled", "1") != "1":
            return False
        return cfg.get(category, "1") == "1"


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

