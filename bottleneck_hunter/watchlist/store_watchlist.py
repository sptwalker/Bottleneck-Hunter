"""WatchlistStore mixin：Watchlist CRUD、User Preferences。"""

from __future__ import annotations

import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso, normalize_market
from bottleneck_hunter.watchlist.tier_limits import derive_tier_caps


class _WatchlistMixin:
    def _effective_tier_caps(self) -> dict[str, int]:
        """本 store 实例生效的分档容量。API 层通过 for_user 注入 _tier_caps；
        未注入时回退到默认派生（保证脚本/后台直连也有合理限额）。"""
        caps = getattr(self, "_tier_caps", None)
        return caps if caps else derive_tier_caps()

    def add(self, entry: dict) -> str:
        """Add a stock to the watchlist. Returns entry id. Raises ValueError on capacity overflow."""
        conn = self._connect()
        try:
            from bottleneck_hunter.watchlist.store_base import normalize_ticker
            if entry.get("ticker"):
                entry = {**entry, "ticker": normalize_ticker(entry["ticker"], entry.get("market", ""))}
            tier = entry.get("tier", "track")
            caps = self._effective_tier_caps()
            tier_cap = caps.get(tier, caps.get("track", 12))
            total_cap = sum(caps.values())
            # 检查分档容量（_filtered = user + market，未 scope 市场时退化为仅 user）
            q, p = self._filtered("SELECT COUNT(*) AS cnt FROM watchlist WHERE tier = ?", (tier,))
            row = conn.execute(q, p).fetchone()
            if row["cnt"] >= tier_cap:
                raise ValueError(f"Tier '{tier}' is full (max {tier_cap})")
            # 检查总容量（按市场，若已 scope）
            q, p = self._filtered("SELECT COUNT(*) AS cnt FROM watchlist")
            total = conn.execute(q, p).fetchone()
            if total["cnt"] >= total_cap:
                raise ValueError(f"Watchlist is full (max {total_cap})")
            # 检查重复（ticker 唯一，与市场无关）
            q, p = self._user_filter("SELECT id FROM watchlist WHERE ticker = ?", (entry["ticker"],))
            existing = conn.execute(q, p).fetchone()
            if existing:
                raise ValueError(f"Ticker '{entry['ticker']}' already in watchlist")

            entry_id = entry.get("id") or uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                f"""INSERT INTO watchlist
                   (id, ticker, company_name, company_name_cn, market, tier, tier_rank,
                    composite_score, source, source_analysis_id, sector, bottleneck_node,
                    added_at, updated_at, notes, is_active{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    entry_id,
                    entry["ticker"],
                    entry.get("company_name", entry["ticker"]),
                    entry.get("company_name_cn", ""),
                    normalize_market(entry.get("market")),
                    tier,
                    entry.get("tier_rank", 0),
                    entry.get("composite_score", 0.0),
                    entry.get("source", "manual"),
                    entry.get("source_analysis_id"),
                    entry.get("sector", ""),
                    entry.get("bottleneck_node", ""),
                    now,
                    now,
                    entry.get("notes", ""),
                    1,
                ) + self._user_insert_params(),
            )
            conn.commit()
            return entry_id
        finally:
            conn.close()


    def remove(self, entry_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._user_filter("DELETE FROM watchlist WHERE id = ?", (entry_id,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def update(self, entry_id: str, **fields) -> bool:
        if not fields:
            return False
        allowed = {"tier", "tier_rank", "composite_score", "notes", "is_active", "updated_at",
                   "source_analysis_id", "bottleneck_node", "sector", "company_name_cn"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        if "updated_at" not in fields:
            parts.append("updated_at = ?")
            vals.append(_now_iso())
        vals.append(entry_id)
        conn = self._connect()
        try:
            q, p = self._user_filter(f"UPDATE watchlist SET {', '.join(parts)} WHERE id = ?", tuple(vals))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


    def get(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM watchlist WHERE id = ?", (entry_id,))
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_by_ticker(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM watchlist WHERE ticker = ?", (ticker,))
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def list_all(self, tier: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if tier:
                q, p = self._filtered(
                    "SELECT * FROM watchlist WHERE tier = ? ORDER BY composite_score DESC, tier_rank ASC",
                    (tier,),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._filtered(
                    "SELECT * FROM watchlist ORDER BY tier, composite_score DESC, tier_rank ASC"
                )
                rows = conn.execute(q, p).fetchall()
            entries = [dict(r) for r in rows]
            # 行业统一为细中文：已是中文的保留；英文/粗名(如 "Technology") 用 company_profiles.industry 映射
            # （focus/normal 入库未带 sector，且 yfinance sector 偏粗英文——统一显示细中文）
            from bottleneck_hunter.watchlist.industry_zh import to_zh_sector
            tickers = [e["ticker"] for e in entries]
            profs: dict[str, dict] = {}
            if tickers:
                ph = ",".join("?" * len(tickers))
                pq = f"SELECT ticker, sector, industry FROM company_profiles WHERE ticker IN ({ph})"
                pp: tuple = tuple(tickers)
                if self._user_id:
                    pq += " AND user_id = ?"
                    pp = pp + (self._user_id,)
                profs = {r["ticker"]: {"sector": r["sector"] or "", "industry": r["industry"] or ""}
                         for r in conn.execute(pq, pp).fetchall()}
            for e in entries:
                pf = profs.get(e["ticker"], {})
                e["sector"] = to_zh_sector(e.get("sector", ""), pf.get("industry", ""), pf.get("sector", ""))
            return entries
        finally:
            conn.close()


    def count_by_tier(self) -> dict[str, int]:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT tier, COUNT(*) AS cnt FROM watchlist GROUP BY tier")
            rows = conn.execute(q, p).fetchall()
            result = {"focus": 0, "normal": 0, "track": 0}
            for r in rows:
                result[r["tier"]] = r["cnt"]
            return result
        finally:
            conn.close()


    def get_tickers(self) -> list[str]:
        conn = self._connect()
        try:
            q, p = self._filtered("SELECT ticker FROM watchlist WHERE is_active = 1")
            rows = conn.execute(q, p).fetchall()
            return [r["ticker"] for r in rows]
        finally:
            conn.close()


    def get_tickers_by_market(self) -> dict[str, list[str]]:
        """按市场分组返回活跃 ticker（去重）。

        unbound store(全局 job)取全体并集时，多个用户观察同一支票会出现重复行——
        用 DISTINCT 去重，避免全局拉取的 stocks_total 虚高、下游拿到重复票。
        """
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT DISTINCT ticker, market FROM watchlist WHERE is_active = 1"
            )
            rows = conn.execute(q, p).fetchall()
            result: dict[str, list[str]] = {}
            seen: set = set()
            for r in rows:
                mkt = r["market"] or "us_stock"
                key = (mkt, r["ticker"])
                if key in seen:
                    continue
                seen.add(key)
                result.setdefault(mkt, []).append(r["ticker"])
            return result
        finally:
            conn.close()


    def _pref_key(self, key: str) -> str:
        """偏好 key 按市场命名空间化：避免 A股/美股 偏好互相覆盖（user_preferences.key 全局 UNIQUE，
        无法用独立 market 列区分同名 key）。未 scope 市场时保持原样。"""
        return f"{self._market}::{key}" if self._market else key

    def save_preference(self, key: str, value: str, category: str = "general") -> str:
        key = self._pref_key(key)
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT id FROM user_preferences WHERE key = ?", (key,)
            )
            existing = conn.execute(q, p).fetchone()
            if existing:
                q, p = self._user_filter(
                    "UPDATE user_preferences SET value = ?, category = ?, updated_at = ? WHERE key = ?",
                    (value, category, _now_iso(), key),
                )
                conn.execute(q, p)
                conn.commit()
                return existing["id"]
            pid = uuid.uuid4().hex[:12]
            conn.execute(
                f"INSERT INTO user_preferences (id, key, value, category, updated_at{self._user_insert_cols()}) VALUES (?,?,?,?,?{self._user_insert_vals()})",
                (pid, key, value, category, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return pid
        finally:
            conn.close()


    def get_preferences(self, category: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            # 市场 scope 时只取本市场偏好（key 前缀 "market::"），并剥前缀返回
            if self._market:
                prefix = f"{self._market}::"
                if category:
                    q, p = self._user_filter(
                        "SELECT * FROM user_preferences WHERE category = ?", (category,))
                else:
                    q, p = self._user_filter("SELECT * FROM user_preferences")
                rows = conn.execute(q, p).fetchall()
                out = []
                for r in rows:
                    k = str(r["key"] or "")
                    if not k.startswith(prefix):  # 用 Python 前缀匹配，避开 LIKE 的 '_' 通配陷阱
                        continue
                    d = dict(r)
                    d["key"] = k[len(prefix):]
                    out.append(d)
                return out
            if category:
                q, p = self._user_filter(
                    "SELECT * FROM user_preferences WHERE category = ?", (category,)
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter("SELECT * FROM user_preferences")
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_preference(self, key: str, default: str = "") -> str:
        key = self._pref_key(key)
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT value FROM user_preferences WHERE key = ?", (key,)
            )
            row = conn.execute(q, p).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

