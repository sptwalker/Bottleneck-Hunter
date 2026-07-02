"""WatchlistStore mixin：行情快照 / 新闻 / SEC / 内部交易 / 期权 / 财报 / 机构持仓 / 分析师评级 / 企业基本面。"""

from __future__ import annotations

import json
import uuid

from bottleneck_hunter.watchlist.store_base import _now_iso


class _MarketDataMixin:
    def save_snapshots(self, snapshots: list[dict]) -> int:
        if not snapshots:
            return 0
        with self._write_conn() as conn:
            count = 0
            for s in snapshots:
                conn.execute(
                    f"""INSERT OR REPLACE INTO market_snapshots
                       (ticker, date, open, high, low, close, volume, market_cap,
                        pe_ratio, change_pct, rsi_14, macd, macd_signal, macd_hist,
                        sma_20, sma_50, fetched_at, market,
                        data_quality, quality_notes{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        s["ticker"], s["date"], s.get("open"), s.get("high"),
                        s.get("low"), s.get("close"), s.get("volume"),
                        s.get("market_cap"), s.get("pe_ratio"), s.get("change_pct"),
                        s.get("rsi_14"), s.get("macd"), s.get("macd_signal"),
                        s.get("macd_hist"), s.get("sma_20"), s.get("sma_50"),
                        s.get("fetched_at", _now_iso()),
                        s.get("market", "us_stock"),
                        s.get("data_quality", "normal"),
                        s.get("quality_notes", ""),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count


    def get_snapshots(self, ticker: str, days: int = 90) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, days),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def get_latest_snapshot(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


    def get_benchmark_return(self, start_date: str, end_date: str,
                             benchmark: str = "SPY") -> float:
        """获取基准指数在指定时段的收益率百分比"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT date, close FROM market_snapshots
                   WHERE ticker = ? AND date >= ? AND date <= ?
                   ORDER BY date ASC""",
                (benchmark, start_date, end_date),
            )
            rows = conn.execute(q, p).fetchall()
            if len(rows) < 2:
                return 0.0
            first_close = rows[0]["close"]
            last_close = rows[-1]["close"]
            if not first_close:
                return 0.0
            return round((last_close / first_close - 1) * 100, 2)
        finally:
            conn.close()


    def save_news(self, items: list[dict]) -> int:
        if not items:
            return 0
        conn = self._connect()
        try:
            count = 0
            for n in items:
                nid = n.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO news_digest
                       (id, ticker, date, title, summary, sentiment, sentiment_score,
                        source_url, source_name, llm_analysis, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        nid, n["ticker"], n["date"], n["title"],
                        n.get("summary", ""), n.get("sentiment", ""),
                        n.get("sentiment_score", 0.0), n.get("source_url", ""),
                        n.get("source_name", ""), n.get("llm_analysis", ""),
                        n.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()


    def get_news(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM news_digest WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_filings(self, filings: list[dict]) -> int:
        if not filings:
            return 0
        conn = self._connect()
        try:
            count = 0
            for f in filings:
                fid = f.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO sec_filings
                       (id, ticker, filing_type, filed_date, title, summary, url,
                        is_insider_trade, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        fid, f["ticker"], f["filing_type"], f["filed_date"],
                        f.get("title", ""), f.get("summary", ""), f.get("url", ""),
                        1 if f.get("is_insider_trade") else 0,
                        f.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()


    def get_filings(self, ticker: str, filing_type: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if filing_type:
                q, p = self._user_filter(
                    "SELECT * FROM sec_filings WHERE ticker = ? AND filing_type = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, filing_type, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM sec_filings WHERE ticker = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_insider_trades(self, trades: list[dict]) -> int:
        if not trades:
            return 0
        conn = self._connect()
        try:
            count = 0
            for t in trades:
                tid = t.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO insider_trades
                       (id, ticker, insider_name, insider_title, transaction_type,
                        shares, price, total_value, date, source_filing_id, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        tid, t["ticker"], t["insider_name"], t.get("insider_title", ""),
                        t.get("transaction_type", ""), t.get("shares", 0),
                        t.get("price"), t.get("total_value"), t["date"],
                        t.get("source_filing_id", ""), t.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()


    def get_insider_trades(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM insider_trades WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_options(self, activities: list[dict]) -> int:
        if not activities:
            return 0
        conn = self._connect()
        try:
            count = 0
            for a in activities:
                aid = a.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO options_activity
                       (id, ticker, date, unusual_volume, put_call_ratio,
                        total_call_volume, total_put_volume, max_oi_strike,
                        max_oi_expiry, notable_trades, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        aid, a["ticker"], a["date"],
                        1 if a.get("unusual_volume") else 0,
                        a.get("put_call_ratio"), a.get("total_call_volume", 0),
                        a.get("total_put_volume", 0), a.get("max_oi_strike"),
                        a.get("max_oi_expiry", ""),
                        json.dumps(a.get("notable_trades", []), ensure_ascii=False),
                        a.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()


    def get_options(self, ticker: str, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM options_activity WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("notable_trades"), str):
                    try:
                        d["notable_trades"] = json.loads(d["notable_trades"])
                    except (json.JSONDecodeError, TypeError):
                        d["notable_trades"] = []
                result.append(d)
            return result
        finally:
            conn.close()


    def save_earnings(self, reports: list[dict]) -> int:
        if not reports:
            return 0
        conn = self._connect()
        try:
            count = 0
            for e in reports:
                eid = e.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO earnings_reports
                       (id, ticker, report_date, fiscal_quarter, eps_actual,
                        eps_estimate, eps_surprise_pct, revenue_actual,
                        revenue_estimate, guidance, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        eid, e["ticker"], e["report_date"],
                        e.get("fiscal_quarter", ""),
                        e.get("eps_actual"), e.get("eps_estimate"),
                        e.get("eps_surprise_pct"), e.get("revenue_actual"),
                        e.get("revenue_estimate"), e.get("guidance", ""),
                        e.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()


    def get_earnings(self, ticker: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM earnings_reports WHERE ticker = ? ORDER BY report_date DESC",
                (ticker,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_institutional_holders(self, ticker: str, holders: list[dict]) -> int:
        """保存机构持仓数据。使用 INSERT OR REPLACE 按 (ticker, holder_name, date) 去重。"""
        if not holders:
            return 0
        with self._write_conn() as conn:
            count = 0
            for h in holders:
                conn.execute(
                    f"""INSERT OR REPLACE INTO institutional_holders
                       (ticker, holder_name, shares, value, pct_held, date, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        ticker,
                        h.get("holder_name", ""),
                        h.get("shares", 0),
                        h.get("value", 0.0),
                        h.get("pct_held", 0.0),
                        h.get("date", ""),
                        h.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count


    def get_institutional_holders(self, ticker: str, limit: int = 50) -> list[dict]:
        """获取指定 ticker 的机构持仓，按持仓比例降序。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM institutional_holders WHERE ticker = ? ORDER BY pct_held DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_analyst_ratings(self, ticker: str, ratings: list[dict]) -> int:
        """保存分析师评级数据。使用 INSERT OR REPLACE 按 (ticker, firm, date) 去重。"""
        if not ratings:
            return 0
        with self._write_conn() as conn:
            count = 0
            for r in ratings:
                conn.execute(
                    f"""INSERT OR REPLACE INTO analyst_ratings
                       (ticker, firm, rating, target_price, date, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        ticker,
                        r.get("firm", ""),
                        r.get("rating", ""),
                        r.get("target_price"),
                        r.get("date", ""),
                        r.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count


    def get_analyst_ratings(self, ticker: str, limit: int = 50) -> list[dict]:
        """获取指定 ticker 的分析师评级，按日期降序。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM analyst_ratings WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


    def save_company_profile(self, ticker: str, info: dict) -> None:
        """从 yfinance info dict 提取企业基本面并 upsert。"""
        if not info:
            return
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO company_profiles
                   (ticker, raw_json, sector, industry, description, website,
                    employees, country, exchange, currency, fetched_at, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker,
                    json.dumps(info, ensure_ascii=False, default=str),
                    info.get("sector", ""),
                    info.get("industry", ""),
                    info.get("longBusinessSummary", ""),
                    info.get("website", ""),
                    info.get("fullTimeEmployees") or 0,
                    info.get("country", ""),
                    info.get("exchange", ""),
                    info.get("currency", ""),
                    _now_iso(),
                    self._user_id or "",
                ),
            )


    def get_company_profile(self, ticker: str) -> dict | None:
        """获取企业基本面，返回结构化 dict。"""
        conn = self._connect()
        try:
            q = "SELECT * FROM company_profiles WHERE ticker = ?"
            p: tuple = (ticker,)
            if self._user_id:
                q += " AND user_id = ?"
                p = (ticker, self._user_id)
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["raw"] = json.loads(d.pop("raw_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["raw"] = {}
            return d
        finally:
            conn.close()

