"""DataHub CapabilityProvider 实现 — 付费源封装（FMP/Tushare）。

用 resolve_data_source_key 取 key（DB→env），映射到 earnings 规范 dict（对齐 earnings_reports 表列，
save_earnings 可直存）。requests 同步调用放 asyncio.to_thread。
"""

from __future__ import annotations

import asyncio
import logging

import requests

from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
from bottleneck_hunter.data_provider.hub import (
    CAP_EARNINGS,
    CAP_FINANCIALS,
    CAP_INSIDER,
    CAP_NEWS,
    CAP_OPTIONS,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_UA = {"User-Agent": "BottleneckHunter/1.0"}
_FMP = "https://financialmodelingprep.com/stable"


def _get_json(url: str, headers: dict | None = None):
    r = requests.get(url, timeout=_TIMEOUT, headers=headers or _UA)
    r.raise_for_status()
    return r.json()


def _get_json_soft(url: str, headers: dict | None = None):
    """付费档端点用：402/403(计划未含)返回 None，不抛→不触发熔断/换源浪费。其它错误照抛。"""
    r = requests.get(url, timeout=_TIMEOUT, headers=headers or _UA)
    if r.status_code in (402, 403):
        return None
    r.raise_for_status()
    return r.json()


def _f(val, scale: float = 1.0) -> float | None:
    """安全转 float（AlphaVantage 等返回字符串金额，'None'/'-' 视为空）。"""
    if val is None or val in ("None", "-", "", "N/A"):
        return None
    try:
        return round(float(val) * scale, 4)
    except (ValueError, TypeError):
        return None


def _news_id(title: str, url: str) -> str:
    import hashlib
    return hashlib.md5(f"{title}|{url}".encode()).hexdigest()[:12]


def _quarters_yoy(rows: list[dict]) -> list[dict]:
    """rows 已按时间降序，每项含 report_date/revenue_yi/net_profit_yi/gross_margin_pct。
    计算营收/净利同比（当期 vs 4 季度前），返回对齐 QuarterlyDataPoint 的 dict 列表。"""
    for i, q in enumerate(rows):
        if i + 4 < len(rows):
            prev = rows[i + 4]
            rv, pv = q.get("revenue_yi"), prev.get("revenue_yi")
            if rv and pv:
                q["revenue_yoy_pct"] = round((rv / pv - 1) * 100, 2)
            nv, pnv = q.get("net_profit_yi"), prev.get("net_profit_yi")
            if nv and pnv and pnv != 0:
                q["net_profit_yoy_pct"] = round((nv / pnv - 1) * 100, 2)
    return rows


def _quarter_from_date(date_str: str) -> str:
    """从报告日期粗推财季（Q1-Q4）。"""
    try:
        m = int(date_str[5:7])
        return f"Q{(m - 1) // 3 + 1}"
    except (ValueError, IndexError):
        return ""


def _surprise_pct(actual, est) -> float | None:
    if actual is None or est in (None, 0):
        return None
    try:
        return round((float(actual) - float(est)) / abs(float(est)) * 100, 2)
    except (ValueError, ZeroDivisionError):
        return None


class FMPProvider:
    """Financial Modeling Prep — 美股 earnings/深财务/新闻/内部人（含一致预期）。质量首选。"""
    name = "fmp"
    priority = 0
    cap_priority = {CAP_NEWS: 1, CAP_INSIDER: 1}  # earnings/financials 仍 0（质量最高）

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS, CAP_FINANCIALS, CAP_NEWS, CAP_INSIDER}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        key = resolve_data_source_key("fmp", user_id)
        if not key:
            return None
        fn = {
            CAP_EARNINGS: self._fetch_earnings_sync,
            CAP_FINANCIALS: self._fetch_financials_sync,
            CAP_NEWS: self._fetch_news_sync,
            CAP_INSIDER: self._fetch_insider_sync,
        }.get(capability)
        if fn is None:
            return None
        return await asyncio.to_thread(fn, ticker, key)

    def _fetch_financials_sync(self, ticker: str, key: str) -> dict | None:
        # 免费档：income-statement 上限 5 季、analyst-estimates/quote 可用；ratios/news/insider 付费(软失败)。
        inc = _get_json_soft(f"{_FMP}/income-statement?symbol={ticker}&period=quarter&limit=5&apikey={key}")
        if not isinstance(inc, list) or not inc:
            return None
        quarters = []
        for row in inc:
            rev = _f(row.get("revenue"), 1e-8)
            gp = _f(row.get("grossProfit"), 1e-8)
            quarters.append({
                "report_date": row.get("date", ""),
                "revenue_yi": rev,
                "net_profit_yi": _f(row.get("netIncome"), 1e-8),
                "gross_margin_pct": round(gp / rev * 100, 2) if (rev and gp) else None,
            })
        quarters = _quarters_yoy(quarters)
        latest = quarters[0]

        # ratios 多为付费档 → 软失败，roe/负债/现金流留空（由免费兜底或其它源补）
        ratios = _get_json_soft(f"{_FMP}/ratios?symbol={ticker}&period=quarter&limit=1&apikey={key}")
        r0 = ratios[0] if isinstance(ratios, list) and ratios else {}
        roe = _f(r0.get("returnOnEquity"))
        debt = _f(r0.get("debtToEquityRatio") or r0.get("debtEquityRatio"))
        cfps = _f(r0.get("operatingCashFlowPerShare"))

        # analyst-estimates 免费：真一致预期 EPS + 真分析师数
        cons_eps = cons_pe = None
        n_analysts = None
        try:
            est = _get_json_soft(f"{_FMP}/analyst-estimates?symbol={ticker}&period=annual&limit=1&apikey={key}")
            if isinstance(est, list) and est:
                cons_eps = _f(est[0].get("epsAvg"))
                n_analysts = est[0].get("numAnalystsEps")
            if cons_eps:
                q = _get_json_soft(f"{_FMP}/quote?symbol={ticker}&apikey={key}")
                price = _f(q[0].get("price")) if isinstance(q, list) and q else None
                if price:
                    cons_pe = round(price / cons_eps, 2)
        except Exception as e:  # noqa: BLE001
            logger.debug("FMP 一致预期获取失败 (%s): %s", ticker, e)

        return {
            "data_source": "fmp",
            "report_date": latest["report_date"],
            "revenue_yi": latest["revenue_yi"],
            "revenue_yoy_pct": latest.get("revenue_yoy_pct"),
            "net_profit_yi": latest["net_profit_yi"],
            "net_profit_yoy_pct": latest.get("net_profit_yoy_pct"),
            "gross_margin_pct": latest["gross_margin_pct"],
            "roe_pct": round(roe * 100, 2) if roe is not None else None,
            "debt_ratio_pct": round(debt * 100, 2) if debt is not None else None,
            "cashflow_per_share": cfps,
            "consensus_eps": cons_eps,
            "consensus_pe": cons_pe,
            "analyst_rating": None,
            "analyst_report_count": int(n_analysts) if n_analysts else None,
            "quarters": quarters,
        }

    def _fetch_news_sync(self, ticker: str, key: str) -> dict | None:
        rows = _get_json_soft(f"{_FMP}/news/stock?symbols={ticker}&limit=15&apikey={key}")  # 付费档
        if not isinstance(rows, list) or not rows:
            return None
        arts = []
        for n in rows:
            title = n.get("title", "")
            url = n.get("url", "")
            if not title:
                continue
            arts.append({
                "id": _news_id(title, url), "ticker": ticker,
                "date": (n.get("publishedDate", "") or "")[:10], "title": title,
                "summary": n.get("text", "")[:500], "source_url": url,
                "source_name": n.get("site", "FMP"),
            })
        return {"articles": arts} if arts else None

    def _fetch_insider_sync(self, ticker: str, key: str) -> dict | None:
        rows = _get_json_soft(f"{_FMP}/insider-trading/search?symbol={ticker}&limit=20&apikey={key}")  # 付费档
        if not isinstance(rows, list) or not rows:
            return None
        return {"ticker": ticker, "source": "fmp", "trades": rows}

    def _fetch_earnings_sync(self, ticker: str, key: str) -> dict | None:
        # /stable/earnings 一次返回 实际值+一致预期（epsActual/epsEstimated/revenueActual/revenueEstimated）
        url = f"https://financialmodelingprep.com/stable/earnings?symbol={ticker}&apikey={key}"
        r = requests.get(url, timeout=_TIMEOUT, headers=_UA)
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        # 取最近一条“已公布实际值”的财报；若全是未来预期则取最近一条
        published = [x for x in rows if x.get("epsActual") is not None]
        rec = (published or rows)[0]
        eps_a, eps_e = rec.get("epsActual"), rec.get("epsEstimated")
        rev_a, rev_e = rec.get("revenueActual"), rec.get("revenueEstimated")
        date = rec.get("date", "")
        return {
            "ticker": ticker,
            "report_date": date,
            "fiscal_quarter": _quarter_from_date(date),
            "eps_actual": eps_a,
            "eps_estimate": eps_e,
            "eps_surprise_pct": _surprise_pct(eps_a, eps_e),
            "revenue_actual": rev_a,
            "revenue_estimate": rev_e,
            "guidance": "",  # stable earnings 端点无 guidance
        }


class TushareProvider:
    """Tushare Pro — A股 earnings（业绩快报/预告；免费无一致预期）。"""
    name = "tushare"
    priority = 0

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS}

    def markets(self) -> set[str]:
        return {"a_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        if capability != CAP_EARNINGS:
            return None
        token = resolve_data_source_key("tushare", user_id)
        if not token:
            return None
        return await asyncio.to_thread(self._fetch_express_sync, ticker, token)

    def _fetch_express_sync(self, ticker: str, token: str) -> dict | None:
        # Tushare 用 6 位代码 + 交易所后缀（如 000001.SZ / 600000.SH）
        ts_code = _to_ts_code(ticker)
        if not ts_code:
            return None
        r = requests.post("https://api.tushare.pro", timeout=_TIMEOUT, headers=_UA, json={
            "api_name": "express", "token": token,
            "params": {"ts_code": ts_code},
            "fields": "ts_code,ann_date,end_date,diluted_eps,revenue,yoy_sales",
        })
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            return None
        items = data.get("data", {}).get("items", [])
        fields = data.get("data", {}).get("fields", [])
        if not items:
            return None
        row = dict(zip(fields, items[0], strict=False))
        end = str(row.get("end_date") or "")
        report_date = f"{end[:4]}-{end[4:6]}-{end[6:8]}" if len(end) == 8 else end
        return {
            "ticker": ticker,
            "report_date": report_date,
            "fiscal_quarter": _quarter_from_date(report_date),
            "eps_actual": row.get("diluted_eps"),
            "eps_estimate": None,   # 免费档无一致预期
            "eps_surprise_pct": None,
            "revenue_actual": row.get("revenue"),
            "revenue_estimate": None,
            "guidance": "",
        }


class FinnhubProvider:
    """Finnhub — 美股 earnings/news/insider（免费 60/分）。"""
    name = "finnhub"
    priority = 1
    cap_priority = {CAP_NEWS: 0, CAP_INSIDER: 0}
    _BASE = "https://finnhub.io/api/v1"

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS, CAP_NEWS, CAP_INSIDER}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        key = resolve_data_source_key("finnhub", user_id)
        if not key:
            return None
        fn = {CAP_EARNINGS: self._earnings, CAP_NEWS: self._news, CAP_INSIDER: self._insider}.get(capability)
        return await asyncio.to_thread(fn, ticker, key) if fn else None

    def _earnings(self, ticker: str, key: str) -> dict | None:
        rows = _get_json(f"{self._BASE}/stock/earnings?symbol={ticker}&token={key}")
        if not isinstance(rows, list) or not rows:
            return None
        rec = rows[0]  # 最近一期
        eps_a, eps_e = rec.get("actual"), rec.get("estimate")
        return {
            "ticker": ticker, "report_date": rec.get("period", ""),
            "fiscal_quarter": _quarter_from_date(rec.get("period", "")),
            "eps_actual": eps_a, "eps_estimate": eps_e,
            "eps_surprise_pct": _surprise_pct(eps_a, eps_e),
            "revenue_actual": None, "revenue_estimate": None, "guidance": "",
        }

    def _news(self, ticker: str, key: str) -> dict | None:
        import datetime as _dt
        to = _dt.date.today()
        frm = to - _dt.timedelta(days=14)
        rows = _get_json(f"{self._BASE}/company-news?symbol={ticker}&from={frm}&to={to}&token={key}")
        if not isinstance(rows, list) or not rows:
            return None
        arts = []
        for n in rows[:15]:
            title = n.get("headline", "")
            if not title:
                continue
            ts = n.get("datetime", 0)
            date = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            arts.append({
                "id": _news_id(title, n.get("url", "")), "ticker": ticker, "date": date,
                "title": title, "summary": n.get("summary", "")[:500],
                "source_url": n.get("url", ""), "source_name": n.get("source", "Finnhub"),
            })
        return {"articles": arts} if arts else None

    def _insider(self, ticker: str, key: str) -> dict | None:
        data = _get_json(f"{self._BASE}/stock/insider-transactions?symbol={ticker}&token={key}")
        rows = data.get("data") if isinstance(data, dict) else None
        return {"ticker": ticker, "source": "finnhub", "trades": rows} if rows else None


class AlphaVantageProvider:
    """Alpha Vantage — 美股 financials/earnings/news（免费档极紧 ~25/日）。"""
    name = "alphavantage"
    priority = 2
    cap_priority = {CAP_NEWS: 1}
    _BASE = "https://www.alphavantage.co/query"

    def capabilities(self) -> set[str]:
        return {CAP_FINANCIALS, CAP_EARNINGS, CAP_NEWS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        key = resolve_data_source_key("alphavantage", user_id)
        if not key:
            return None
        fn = {CAP_FINANCIALS: self._financials, CAP_EARNINGS: self._earnings, CAP_NEWS: self._news}.get(capability)
        return await asyncio.to_thread(fn, ticker, key) if fn else None

    def _financials(self, ticker: str, key: str) -> dict | None:
        # OVERVIEW 一次拿 TTM 概览（免费省额度）；限流时返回含 Note/Information 字段
        ov = _get_json(f"{self._BASE}?function=OVERVIEW&symbol={ticker}&apikey={key}")
        if not isinstance(ov, dict) or ov.get("Note") or ov.get("Information") or not ov.get("Symbol"):
            return None
        roe = _f(ov.get("ReturnOnEquityTTM"))
        return {
            "data_source": "alphavantage",
            "report_date": ov.get("LatestQuarter", ""),
            "revenue_yi": _f(ov.get("RevenueTTM"), 1e-8),
            "revenue_yoy_pct": _f(ov.get("QuarterlyRevenueGrowthYOY"), 100),
            "net_profit_yi": None,
            "net_profit_yoy_pct": _f(ov.get("QuarterlyEarningsGrowthYOY"), 100),
            "gross_margin_pct": None,  # AV OVERVIEW 无直接毛利率
            "roe_pct": round(roe * 100, 2) if roe is not None else None,
            "debt_ratio_pct": None,
            "cashflow_per_share": None,
            "consensus_eps": _f(ov.get("EPS")),
            "consensus_pe": _f(ov.get("ForwardPE")) or _f(ov.get("PERatio")),
            "analyst_rating": None,
            "analyst_report_count": None,
            "quarters": [],
        }

    def _earnings(self, ticker: str, key: str) -> dict | None:
        d = _get_json(f"{self._BASE}?function=EARNINGS&symbol={ticker}&apikey={key}")
        q = (d or {}).get("quarterlyEarnings") if isinstance(d, dict) else None
        if not q:
            return None
        r = q[0]
        eps_a, eps_e = _f(r.get("reportedEPS")), _f(r.get("estimatedEPS"))
        return {
            "ticker": ticker, "report_date": r.get("fiscalDateEnding", ""),
            "fiscal_quarter": _quarter_from_date(r.get("fiscalDateEnding", "")),
            "eps_actual": eps_a, "eps_estimate": eps_e,
            "eps_surprise_pct": _surprise_pct(eps_a, eps_e),
            "revenue_actual": None, "revenue_estimate": None, "guidance": "",
        }

    def _news(self, ticker: str, key: str) -> dict | None:
        d = _get_json(f"{self._BASE}?function=NEWS_SENTIMENT&tickers={ticker}&limit=15&apikey={key}")
        feed = (d or {}).get("feed") if isinstance(d, dict) else None
        if not feed:
            return None
        arts = []
        for n in feed[:15]:
            title = n.get("title", "")
            if not title:
                continue
            t = n.get("time_published", "")
            date = f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) >= 8 else ""
            arts.append({
                "id": _news_id(title, n.get("url", "")), "ticker": ticker, "date": date,
                "title": title, "summary": (n.get("summary", "") or "")[:500],
                "source_url": n.get("url", ""), "source_name": n.get("source", "AlphaVantage"),
                "sentiment_score": _f(n.get("overall_sentiment_score")) or 0.0,
            })
        return {"articles": arts} if arts else None


class TiingoProvider:
    """Tiingo — 美股 news/financials（fundamentals/news 需付费 add-on，无则软失败）。"""
    name = "tiingo"
    priority = 1
    cap_priority = {CAP_NEWS: 0}
    _BASE = "https://api.tiingo.com"

    def capabilities(self) -> set[str]:
        return {CAP_NEWS, CAP_FINANCIALS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        key = resolve_data_source_key("tiingo", user_id)
        if not key:
            return None
        fn = {CAP_NEWS: self._news, CAP_FINANCIALS: self._financials}.get(capability)
        return await asyncio.to_thread(fn, ticker, key) if fn else None

    def _hdr(self, key: str) -> dict:
        return {**_UA, "Authorization": f"Token {key}", "Content-Type": "application/json"}

    def _news(self, ticker: str, key: str) -> dict | None:
        rows = _get_json_soft(f"{self._BASE}/tiingo/news?tickers={ticker.lower()}&limit=15", self._hdr(key))
        if not isinstance(rows, list) or not rows:
            return None
        arts = []
        for n in rows:
            title = n.get("title", "")
            if not title:
                continue
            arts.append({
                "id": _news_id(title, n.get("url", "")), "ticker": ticker,
                "date": (n.get("publishedDate", "") or "")[:10], "title": title,
                "summary": (n.get("description", "") or "")[:500],
                "source_url": n.get("url", ""), "source_name": n.get("source", "Tiingo"),
            })
        return {"articles": arts} if arts else None

    def _financials(self, ticker: str, key: str) -> dict | None:
        daily = _get_json_soft(f"{self._BASE}/tiingo/fundamentals/{ticker.lower()}/daily", self._hdr(key))
        d0 = daily[0] if isinstance(daily, list) and daily else {}
        if not d0:
            return None
        return {
            "data_source": "tiingo",
            "report_date": d0.get("date", ""),
            "revenue_yi": None, "revenue_yoy_pct": None,
            "net_profit_yi": None, "net_profit_yoy_pct": None,
            "gross_margin_pct": None, "roe_pct": None, "debt_ratio_pct": None,
            "cashflow_per_share": None,
            "consensus_eps": None, "consensus_pe": _f(d0.get("peRatio")),
            "analyst_rating": None, "analyst_report_count": None, "quarters": [],
        }


class PolygonProvider:
    """Polygon.io — 美股 options（免费 5/分，本轮聚焦期权）。"""
    name = "polygon"
    priority = 0
    _BASE = "https://api.polygon.io"

    def capabilities(self) -> set[str]:
        return {CAP_OPTIONS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        if capability != CAP_OPTIONS:
            return None
        key = resolve_data_source_key("polygon", user_id)
        if not key:
            return None
        return await asyncio.to_thread(self._options, ticker, key)

    def _options(self, ticker: str, key: str) -> dict | None:
        import datetime as _dt
        import hashlib
        d = _get_json_soft(f"{self._BASE}/v3/snapshot/options/{ticker}?limit=250&apiKey={key}")
        results = (d or {}).get("results") if isinstance(d, dict) else None
        if not results:
            return None
        call_vol = put_vol = 0
        max_oi = 0.0
        max_oi_strike = None
        notable = []
        for c in results:
            det = c.get("details", {})
            typ = det.get("contract_type")
            vol = (c.get("day") or {}).get("volume", 0) or 0
            oi = c.get("open_interest", 0) or 0
            strike = det.get("strike_price")
            if typ == "call":
                call_vol += vol
            elif typ == "put":
                put_vol += vol
            if oi > max_oi:
                max_oi, max_oi_strike = oi, strike
            if vol > 1000 and typ in ("call", "put"):
                notable.append({"type": typ, "strike": strike, "volume": int(vol),
                                "oi": int(oi), "expiry": det.get("expiration_date", "")})
        pcr = round(put_vol / call_vol, 3) if call_vol > 0 else None
        aid = hashlib.md5(f"{ticker}:opt:{_dt.date.today()}".encode()).hexdigest()[:12]
        return {
            "id": aid, "ticker": ticker, "date": _dt.date.today().strftime("%Y-%m-%d"),
            "unusual_volume": (call_vol + put_vol) > 10000,
            "put_call_ratio": pcr, "total_call_volume": int(call_vol), "total_put_volume": int(put_vol),
            "max_oi_strike": max_oi_strike, "max_oi_expiry": "",
            "notable_trades": notable[:6],
            "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }


class YfinanceOptionsProvider:
    """yfinance 期权兜底 — 复用 options_pipeline 分析逻辑（免费，无 key）。"""
    name = "yfinance"
    priority = 1

    def capabilities(self) -> set[str]:
        return {CAP_OPTIONS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        if capability != CAP_OPTIONS:
            return None
        from bottleneck_hunter.watchlist.options_pipeline import _analyze_options_chain
        return await asyncio.to_thread(_analyze_options_chain, ticker)


def _to_ts_code(ticker: str) -> str:
    """A股 ticker → Tushare ts_code。'600000'→'600000.SH'，'000001'→'000001.SZ'；已带后缀则原样。"""
    t = ticker.strip().upper()
    if "." in t:
        return t
    if len(t) == 6 and t.isdigit():
        return f"{t}.SH" if t[0] == "6" else f"{t}.SZ"
    return ""


def build_providers() -> list:
    return [
        FMPProvider(), FinnhubProvider(), TushareProvider(),
        AlphaVantageProvider(), TiingoProvider(), PolygonProvider(),
        YfinanceOptionsProvider(),
    ]
