"""WatchlistStore mixin：LLM 预算 / 预算配置 / 管道状态 / 自动更新配置。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store_base import _now_iso, _today


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days - 1))).strftime("%Y-%m-%d")


# 自动更新配置默认值（用户未显式设置时的回退）。总开关 + 各分类开关默认全开，陈旧阈值 24h。
AUTO_UPDATE_DEFAULTS: dict[str, str] = {
    "master_enabled": "1",
    "keyed_data": "1",        # 付费数据源(财报/期权，用各自 Key)——客观免费数据已归全局，无每用户开关
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
                """SELECT COUNT(*) AS calls,
                          COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date = ?""",
                (date,),
            )
            row = conn.execute(q, p).fetchone()
            return {"date": date, "calls": row["calls"], "input_tokens": row["input_tokens"],
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
                """SELECT COUNT(*) AS calls,
                          COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date LIKE ?""",
                (f"{prefix}%",),
            )
            row = conn.execute(q, p).fetchone()
            return {"year": year, "month": month, "calls": row["calls"], "input_tokens": row["input_tokens"],
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
        """更新管线状态。pipeline_status 主键是 pipeline_name（全局单例，非每用户——user_id 列
        因 PK 未含它而从不生效），故按 name 全局 upsert，不做用户过滤。"""
        conn = self._connect()
        try:
            conn.execute("INSERT OR IGNORE INTO pipeline_status(pipeline_name) VALUES (?)", (name,))
            allowed = {"last_run_at", "last_status", "last_error", "next_run_at",
                        "stocks_processed", "stocks_total"}
            parts, vals = [], []
            for k, v in fields.items():
                if k in allowed:
                    parts.append(f"{k} = ?")
                    vals.append(v)
            if parts:
                vals.append(name)
                conn.execute(f"UPDATE pipeline_status SET {', '.join(parts)} WHERE pipeline_name = ?", tuple(vals))
            conn.commit()
        finally:
            conn.close()


    def get_pipeline_statuses(self) -> list[dict]:
        """管线状态（全局单例：PK=pipeline_name，一行一管线，全体用户共见）。

        客观数据由全局 job 拉取、状态全局可见；options/earnings 等每用户管线也写同一行(全局
        单例)——即状态反映"最近一次跑"（谁触发的都一样），符合全局数据保障的语义。
        """
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM pipeline_status").fetchall()]
        finally:
            conn.close()


    # ── DataHub 数据源用量统计（全局表，不经 _user_filter） ──

    def record_ds_call(self, source: str, capability: str, market: str, ok: bool,
                       latency_ms: float = 0.0, rows: int = 0, last_error: str = "") -> None:
        """记一次数据源调用，按 日期×源×能力×市场 UPSERT 累加。失败只 debug 不抛。"""
        try:
            with self._write_conn() as conn:
                conn.execute(
                    """INSERT INTO datasource_stats
                       (date, source, capability, market, calls, ok, fail, latency_sum, rows, last_error, updated_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date, source, capability, market) DO UPDATE SET
                         calls = calls + 1,
                         ok = ok + excluded.ok,
                         fail = fail + excluded.fail,
                         latency_sum = latency_sum + excluded.latency_sum,
                         rows = rows + excluded.rows,
                         last_error = CASE WHEN excluded.last_error != '' THEN excluded.last_error ELSE datasource_stats.last_error END,
                         updated_at = excluded.updated_at""",
                    (_today(), source, capability, market,
                     1 if ok else 0, 0 if ok else 1, float(latency_ms), int(rows),
                     last_error[:200], _now_iso()),
                )
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).debug("record_ds_call 失败: %s", e)

    def get_ds_stats(self, days: int = 7) -> list[dict]:
        """最近 days 天的明细行（按日期倒序）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM datasource_stats WHERE date >= ? ORDER BY date DESC, source",
                (_days_ago(days),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_ds_stats_by_source(self, days: int = 7) -> list[dict]:
        """按源聚合最近 days 天：总调用/成功/失败/成功率/均延迟/行数。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT source,
                          SUM(calls) AS calls, SUM(ok) AS ok, SUM(fail) AS fail,
                          SUM(rows) AS rows,
                          CASE WHEN SUM(calls) > 0 THEN ROUND(100.0 * SUM(ok) / SUM(calls), 1) ELSE 0 END AS ok_rate,
                          CASE WHEN SUM(ok) > 0 THEN ROUND(SUM(latency_sum) / SUM(ok), 1) ELSE 0 END AS avg_latency_ms,
                          MAX(date) AS last_date
                   FROM datasource_stats WHERE date >= ?
                   GROUP BY source ORDER BY calls DESC""",
                (_days_ago(days),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_stale_tickers(self, max_age_hours: int = 48, include_never_fetched: bool = True) -> list[dict]:
        """返回超过 max_age_hours 未刷新的活跃 ticker 列表。

        判据用 fetched_at（上次抓取时间），不是 ms.date（K线交易日）——后者在周末/节假日/
        数据源回补旧 bar 时会合理地"旧"，导致刚一键刷新过仍误报"未更新"。

        include_never_fetched：
        - True（默认，供调度器兜底刷新）：包含从未抓取（last_fetched IS NULL）的标的，让它们被补抓。
        - False（供"超过N小时未更新"用户提示）：只算真有旧数据(>N 小时)的；刚添加、尚未抓取的
          标的 fetched_at 为 NULL，其数据"0 小时"而非"超过48小时"，不应被误报为陈旧。
        """
        conn = self._connect()
        try:
            null_clause = "last_fetched IS NULL OR " if include_never_fetched else "last_fetched IS NOT NULL AND "
            q, p = self._user_filter(
                f"""
                SELECT w.ticker, w.company_name, w.market,
                       MAX(ms.fetched_at) AS last_fetched
                FROM watchlist w
                LEFT JOIN market_snapshots ms ON w.ticker = ms.ticker
                WHERE w.is_active = 1
                GROUP BY w.ticker
                HAVING {null_clause}datetime(last_fetched) < datetime('now', ?)
                """,
                (f"-{max_age_hours} hours",),
                table="w",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

