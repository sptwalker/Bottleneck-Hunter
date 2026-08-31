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
    CAP_KB,
    CAP_MACRO_EDB,
    CAP_NARRATIVE,
    CAP_NEWS,
    CAP_OPTIONS,
    CAP_RESEARCH,
    CAP_SCREEN,
    CAP_VALUATION,
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
    """付费/限流端点用：402/403(计划未含)、429(限流)返回 None，不抛→不误触熔断/浪费换源。其它错误照抛。"""
    r = requests.get(url, timeout=_TIMEOUT, headers=headers or _UA)
    if r.status_code in (402, 403, 429):
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
    """Financial Modeling Prep — 美股 earnings/深财务/新闻（含一致预期）。质量首选。"""
    name = "fmp"
    priority = 0
    cap_priority = {CAP_NEWS: 1}  # earnings/financials 仍 0（质量最高）

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS, CAP_FINANCIALS, CAP_NEWS}

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
        rev_a, rev_e = _f(rec.get("revenueActual"), 1e-8), _f(rec.get("revenueEstimated"), 1e-8)  # USD→亿，对齐 revenue_yi
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
            "revenue_actual": _f(row.get("revenue"), 1e-8),  # 元→亿，对齐 revenue_yi
            "revenue_estimate": None,
            "guidance": "",
        }


class FinnhubProvider:
    """Finnhub — 美股 earnings/news（免费 60/分）。"""
    name = "finnhub"
    priority = 1
    cap_priority = {CAP_NEWS: 0}
    _BASE = "https://finnhub.io/api/v1"

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS, CAP_NEWS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        key = resolve_data_source_key("finnhub", user_id)
        if not key:
            return None
        fn = {CAP_EARNINGS: self._earnings, CAP_NEWS: self._news}.get(capability)
        return await asyncio.to_thread(fn, ticker, key) if fn else None

    def _earnings(self, ticker: str, key: str) -> dict | None:
        rows = _get_json_soft(f"{self._BASE}/stock/earnings?symbol={ticker}&token={key}")
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
        rows = _get_json_soft(f"{self._BASE}/company-news?symbol={ticker}&from={frm}&to={to}&token={key}")
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
        ov = _get_json_soft(f"{self._BASE}?function=OVERVIEW&symbol={ticker}&apikey={key}")
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
            "consensus_eps": None,  # OVERVIEW.EPS 是 trailing TTM，非前瞻一致预期，不冒充（避免覆写 yfinance forward）
            "consensus_pe": _f(ov.get("ForwardPE")),  # 只取前瞻 PE；trailing PERatio 一律留空
            "analyst_rating": None,
            "analyst_report_count": None,
            "quarters": [],
        }

    def _earnings(self, ticker: str, key: str) -> dict | None:
        d = _get_json_soft(f"{self._BASE}?function=EARNINGS&symbol={ticker}&apikey={key}")
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
        d = _get_json_soft(f"{self._BASE}?function=NEWS_SENTIMENT&tickers={ticker}&limit=15&apikey={key}")
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
            "consensus_eps": None, "consensus_pe": None,  # Tiingo daily 的 peRatio 是 trailing，不冒充一致预期
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


_YJBB_CACHE: dict[str, object] = {}


def _latest_yjbb():
    """返回 (报告期'YYYY-MM-DD', df)：最近一个有数据的季度业绩报表。模块级按季度缓存。失败 (None,None)。"""
    try:
        import akshare as ak
    except ImportError:
        return None, None
    from datetime import date
    today = date.today().isoformat()
    cands = sorted(
        (f"{yy}-{md}" for yy in (date.today().year, date.today().year - 1)
         for md in ("12-31", "09-30", "06-30", "03-31") if f"{yy}-{md}" <= today),
        reverse=True,
    )
    for c in cands[:5]:
        d = c.replace("-", "")
        if d in _YJBB_CACHE:
            return c, _YJBB_CACHE[d]
        try:
            df = ak.stock_yjbb_em(date=d)
            if df is not None and not df.empty:
                _YJBB_CACHE[d] = df
                return c, df
        except Exception:  # noqa: BLE001
            continue
    return None, None


class AkshareEarningsProvider:
    """akshare 业绩报表 — A股 earnings 免费兜底（仅实际值 eps/营收，无机构一致预期）。

    排在付费 Tushare(priority 0) 之后，保证无 token 用户 A股 earnings 也不为空。
    stock_yjbb_em 一次返回全市场，按季度缓存，批量取数时各 ticker 复用同一份。
    """
    name = "akshare"
    priority = 1

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS}

    def markets(self) -> set[str]:
        return {"a_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        if capability != CAP_EARNINGS:
            return None
        return await asyncio.to_thread(self._fetch_sync, ticker)

    def _fetch_sync(self, ticker: str) -> dict | None:
        code = "".join(ch for ch in ticker if ch.isdigit())[:6]
        if len(code) != 6:
            return None
        report_date, df = _latest_yjbb()
        if df is None:
            return None
        code_col = next((c for c in df.columns if "股票代码" in c), None)
        eps_col = next((c for c in df.columns if "每股收益" in c), None)
        rev_col = next((c for c in df.columns if "营业总收入" in c), None)
        if not code_col:
            return None
        row = df[df[code_col] == code]
        if row.empty:
            return None
        r = row.iloc[0]
        return {
            "ticker": ticker,
            "report_date": report_date,
            "fiscal_quarter": _quarter_from_date(report_date),
            "eps_actual": _f(r[eps_col]) if eps_col else None,
            "eps_estimate": None,          # A股免费无机构一致预期（诚实边界）
            "eps_surprise_pct": None,
            "revenue_actual": _f(r[rev_col], 1e-8) if rev_col else None,  # 元→亿
            "revenue_estimate": None,
            "guidance": "",
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


def _map_gangtise_financials(fin: dict, forecast: dict | None, market: str = "a_stock") -> dict:
    """纯映射（可单测，不碰网络）：Gangtise 利润表行 + 一致预期 → 规范 CAP_FINANCIALS dict。

    金额 元→亿（1e-8）。A股利润表无 grossProfit 字段，毛利率 =(营收-营业成本)/营收 自算
    （比率与金额刻度无关，故即便 1e-8 假设有偏毛利率仍准）。美股同接口同刻度，币种记 USD。
    ponytail: 金额刻度按「原始=元/美元本币」假设，真实连通后若量级不符，唯一校准点是此处 1e-8。
    """
    row = fin["rows"][0]
    # 字段随市场略异（活体实测 2026-08）：opRev(营业收入)/opCost(营业成本) 三市场同名；净利归母
    #   · A股/港股 = netProfitAttrParent；美股 = netProfitParent。取值链两者都覆盖，回退裸 netProfit。
    # 毛利率优先用接口直供 grossProfit（美/港股有）；A股无该字段时用「营收-营业成本」自算。
    # totalOpCost 含税金/费用是营业利润口径，不能拿来算毛利率。
    rev = _f(row.get("opRev") or row.get("totalOpRev"), 1e-8)
    cost = _f(row.get("opCost"), 1e-8)
    net = _f(row.get("netProfitAttrParent") or row.get("netProfitParent") or row.get("netProfit"), 1e-8)
    gross = _f(row.get("grossProfit"), 1e-8)
    if gross is not None and rev:
        gm = round(gross / rev * 100, 2)
    elif rev and cost is not None:
        gm = round((rev - cost) / rev * 100, 2)
    else:
        gm = None
    cons_eps = cons_pe = None
    if forecast and forecast.get("forecasts"):
        f0 = forecast["forecasts"][0]  # 最近发布日、最近预测年
        cons_eps = _f(f0.get("eps"))
        cons_pe = _f(f0.get("pe"))  # 券商前瞻 PE = 一致预期
    return {
        "data_source": "gangtise",
        "currency": "USD" if market == "us_stock" else "CNY",  # 金额币种（revenue_yi/net_profit_yi 亿本币）
        "report_date": fin.get("report_date", ""),
        "revenue_yi": rev,
        "revenue_yoy_pct": None,   # ponytail: 累计口径同比需拉多期，起步留空；接多期后用 _quarters_yoy
        "net_profit_yi": net,
        "net_profit_yoy_pct": None,
        "gross_margin_pct": gm,
        "roe_pct": None,           # 一致预期 roe 是前瞻值，语义≠实际 roe_pct，不混入
        "debt_ratio_pct": None,
        "cashflow_per_share": None,
        "consensus_eps": cons_eps,
        "consensus_pe": cons_pe,
        "analyst_rating": None,
        "analyst_report_count": None,
        "quarters": [],
    }


def _map_gangtise_valuation(raw: dict | None) -> dict | None:
    """纯映射（可单测，不碰网络）：fetch_valuation 原始 {indicator:{value,percentile,as_of}} →
    规范 CAP_VALUATION dict，键用下游消费者约定名（pe_ttm/pb_mrq/peg + *_percentile）。

    percentile 为近 3 年窗内分位（0~100，越低越便宜）；缺某指标则该组键缺省（不填 None 桩），
    整体全空 → None。as_of 取任一指标的最新交易日（同批同日）。
    """
    if not raw:
        return None
    key_map = {"peTtm": ("pe_ttm", "pe_ttm_percentile"),
               "pbMrq": ("pb_mrq", "pb_mrq_percentile"),
               "peg":   ("peg", "peg_percentile")}
    out: dict = {"data_source": "gangtise"}
    as_of = ""
    for ind, (vk, pk) in key_map.items():
        rec = raw.get(ind)
        if not rec:
            continue
        out[vk] = rec.get("value")
        out[pk] = rec.get("percentile")
        as_of = as_of or (rec.get("as_of") or "")
    if len(out) == 1:   # 只剩 data_source，无任何指标
        return None
    out["as_of"] = as_of
    return out


class GangtiseProvider:
    """Gangtise 投研 — A股基本面（CAP_FINANCIALS）+ 全市场 EDB 宏观（CAP_MACRO_EDB）。

    单 provider 多能力：hub 按 name 建 _states，故 gangtise 的所有域挂同一 name="gangtise"，
    按 capability 分市场（supports 里区分）。凭据统一走 resolve_gangtise_credentials。
    priority=0：带券商一致预期 + EDB 官方口径，质量高于免费兜底。
    - CAP_FINANCIALS：仅 a_stock（_sec_code 只做 A股 6位直通；港美股码制留待第二市场）。
    - CAP_MACRO_EDB：a_stock+us_stock（按 market 取一组 EDB 指标，ticker 位忽略——与经典
      「按 ticker 取一条」的唯一形态差异，见 hub.py CAP_MACRO_EDB 注释）。
    ponytail: 不认领 CAP_EARNINGS——A股 earnings 实际值已由 akshare(priority1) 供给；一致预期
      已并入 financials 的 consensus_eps/pe。补 A股 earnings 一致预期的上升路径：拆 CAP_EARNINGS
      merge 而非单源覆盖，再放开这里。
    """
    name = "gangtise"
    priority = 0

    def capabilities(self) -> set[str]:
        return {CAP_FINANCIALS, CAP_MACRO_EDB, CAP_RESEARCH, CAP_KB, CAP_VALUATION}

    def markets(self) -> set[str]:
        return {"a_stock", "us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        if capability == CAP_FINANCIALS:
            return market in ("a_stock", "us_stock")   # A股 6位直通；美股经 securities/search 解析 .O/.N
        if capability == CAP_VALUATION:
            return market == "a_stock"   # 实测仅 A股有估值分位覆盖；美股/港股 code=120001，不认领
        if capability in (CAP_MACRO_EDB, CAP_RESEARCH, CAP_KB):
            return market in ("a_stock", "us_stock")    # 研报覆盖 A/H/US/中概；KB 与市场无关但按 market 门控
        return False

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        from bottleneck_hunter.data_provider.data_source_catalog import resolve_gangtise_credentials
        if capability not in (CAP_FINANCIALS, CAP_MACRO_EDB, CAP_RESEARCH, CAP_KB, CAP_VALUATION):
            return None
        creds = resolve_gangtise_credentials(user_id)
        if not creds:
            return None
        ak, sk = creds
        if capability == CAP_MACRO_EDB:
            from bottleneck_hunter.data_provider.gangtise_edb_indicators import indicators_for_market
            ids = [v[0] for v in indicators_for_market(market).values()]
            if not ids:
                return None
            return await asyncio.to_thread(self._fetch_edb_sync, ak, sk, ids, market)
        if capability == CAP_RESEARCH:
            return await asyncio.to_thread(self._fetch_research_sync, ak, sk, ticker, market)
        if capability == CAP_KB:
            return await asyncio.to_thread(self._fetch_kb_sync, ak, sk, ticker)
        if capability == CAP_VALUATION:
            return await asyncio.to_thread(self._fetch_valuation_sync, ak, sk, ticker, market)
        return await asyncio.to_thread(self._fetch_sync, ak, sk, ticker, market)

    def _fetch_valuation_sync(self, ak, sk, ticker, market) -> dict | None:
        """估值分位（PE/PB/PEG 近3年窗内分位）。仅 A股；映射为规范 CAP_VALUATION dict。"""
        from bottleneck_hunter.data_provider import gangtise_client as gc
        raw = gc.fetch_valuation(ak, sk, ticker, market)
        return _map_gangtise_valuation(raw)

    def _fetch_research_sync(self, ak, sk, ticker, market) -> dict | None:
        """券商研报证据：近 180 日该标的深度/业绩点评研报（按发布日降序，取前 5）。

        美股走外资 foreign-report、A股走中资 broker-report。llm_tag 先筛深度/点评；
        若无（长尾标的无深度研报）则退回不加标签取全部，避免空手。
        """
        from datetime import date, timedelta

        from bottleneck_hunter.data_provider import gangtise_client as gc
        today = date.today()
        start = (today - timedelta(days=180)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        foreign = market == "us_stock"
        code = gc._resolve_gts_code(ak, sk, ticker, market)
        reports = gc.fetch_research(ak, sk, securities=[code], start=start, end=end,
                                    foreign=foreign, llm_tag_list=["inDepth", "earningsReview"])
        if not reports:
            reports = gc.fetch_research(ak, sk, securities=[code], start=start, end=end, foreign=foreign)
        if not reports:
            return None
        reports.sort(key=lambda r: r.get("publish_date", ""), reverse=True)
        return {"reports": reports[:5]}

    def _fetch_kb_sync(self, ak, sk, query) -> dict | None:
        """知识库 RAG：以 ticker 槽承载的语义 query 检索片段（取回 6 片段）。"""
        from bottleneck_hunter.data_provider import gangtise_client as gc
        snips = gc.fetch_kb(ak, sk, str(query or ""), top=6)
        return {"snippets": snips} if snips else None

    def _fetch_edb_sync(self, ak, sk, ids, market) -> dict | None:
        from datetime import date, timedelta

        from bottleneck_hunter.data_provider import gangtise_client as gc
        today = date.today()
        # 取近 400 天覆盖月频序列至少 2 个非空点（算 change_pct）
        raw = gc.fetch_edb(ak, sk, ids,
                           (today - timedelta(days=400)).strftime("%Y-%m-%d"),
                           today.strftime("%Y-%m-%d"))
        return _map_gangtise_edb(raw, market) or None

    def _fetch_sync(self, ak: str, sk: str, ticker: str, market: str) -> dict | None:
        from datetime import date, timedelta

        from bottleneck_hunter.data_provider import gangtise_client as gc
        fin = gc.fetch_financials(ak, sk, ticker, market)
        if not fin or not fin.get("rows"):
            return None
        forecast = None
        if market == "a_stock":   # 一致预期接口 A股-only（美股返 120001）→ 美股 consensus 留空
            try:  # 一致预期软失败：拿不到不阻断财务主体
                today = date.today()
                forecast = gc.fetch_earnings_forecast(
                    ak, sk, ticker, market,
                    (today - timedelta(days=30)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))
            except Exception as e:  # noqa: BLE001
                logger.debug("Gangtise 一致预期获取失败 (%s): %s", ticker, e)
        return _map_gangtise_financials(fin, forecast, market)


def _map_gangtise_edb(raw: dict, market: str) -> dict:
    """纯映射（可单测）：EDB getData 原始 {id:{latest,prev,date}} → 规范宏观 dict。

    key 用语义名（us_cpi_yoy 等），值经 transform 归一（index100→同比%）。change_pct=最新−前值
    （宏观读数如 CPI/利率/PMI 的「变动」就是百分点差，与 macro_data 的 level 型口径一致）。
    """
    from bottleneck_hunter.data_provider.gangtise_edb_indicators import (
        apply_transform,
        indicators_for_market,
    )
    out: dict[str, dict] = {}
    for key, (fid, label, _scope, transform) in indicators_for_market(market).items():
        rec = raw.get(fid)
        if not rec or rec.get("latest") is None:
            continue
        value = apply_transform(rec["latest"], transform)
        prev = rec.get("prev")
        change = round(value - apply_transform(prev, transform), 2) if prev is not None else 0.0
        out[key] = {"value": value, "change_pct": change, "label": label, "as_of": rec.get("date")}
    return out


class GangtiseNarrativeProvider:
    """Gangtise AI 研报叙事（一页通）——CAP_NARRATIVE，chain/VIP 报告增强段落。

    单独 provider name（不挂 "gangtise"）：agent 调用重、有轮询，失败不应连累核心财务熔断。
    ticker 槽 = 证券码；固定取「一页通(one-pager)」这一同步 agent（最富信息，实测含目标价/机构观点）。
    ponytail: 只接一页通；投资逻辑/同业对比/财报点评(异步600s轮询)按 §10 默认关，
      需要时在 gangtise_client 补子路径 + 这里加 agent_type 分派。
    """
    name = "gangtise_narrative"
    priority = 0

    def capabilities(self) -> set[str]:
        return {CAP_NARRATIVE}

    def markets(self) -> set[str]:
        return {"a_stock", "us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability == CAP_NARRATIVE and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        from bottleneck_hunter.data_provider.data_source_catalog import resolve_gangtise_credentials
        if capability != CAP_NARRATIVE or not (ticker or "").strip():
            return None
        creds = resolve_gangtise_credentials(user_id)
        if not creds:
            return None
        ak, sk = creds
        return await asyncio.to_thread(self._fetch_sync, ak, sk, ticker, market)

    def _fetch_sync(self, ak, sk, ticker, market) -> dict | None:
        from bottleneck_hunter.data_provider import gangtise_client as gc
        code = gc._resolve_gts_code(ak, sk, ticker, market)
        content = gc.fetch_narrative(ak, sk, "one-pager", code)
        return {"agent_type": "one-pager", "content": content} if content else None


class GangtiseScreenProvider:
    """Gangtise 指标选股（A股）——CAP_SCREEN，供应商检索前置漏斗。

    ticker 槽 = 瓶颈环节关键词 → 中信一级板块 sectorId（curated 小表）。在该板块内以「主营业务
    包含关键词」粗筛出候选代码，缩小 chain 深挖集。只做过滤不排序（screener 语义）。
    ponytail: 板块解析用 curated 小表，主营包含做零日期参数过滤（pty_main_bus 无 tradeDate 依赖，
      规避「最近交易日」查 K 线的脆弱性）。上升路径：口语条件(ROE>15&市值>500亿) 需接 skill
      三段式 universe/indicator 解析补 qte_mkt_cptl+tradeDate 等参，别在此堆硬编码。
    """
    name = "gangtise_screen"
    priority = 0

    def capabilities(self) -> set[str]:
        return {CAP_SCREEN}

    def markets(self) -> set[str]:
        return {"a_stock"}   # 板块体系目前 A股（§10 硬边界）

    def supports(self, capability: str, market: str) -> bool:
        return capability == CAP_SCREEN and market == "a_stock"

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        from bottleneck_hunter.data_provider.data_source_catalog import resolve_gangtise_credentials
        from bottleneck_hunter.data_provider.gangtise_sector_ids import sector_id_for
        if capability != CAP_SCREEN or market != "a_stock":
            return None
        kw = (ticker or "").strip()
        sid = sector_id_for(kw)
        if not sid:   # 未收录板块 → 降级（上游仍有 LLM/产业链/akshare 源）
            return None
        creds = resolve_gangtise_credentials(user_id)
        if not creds:
            return None
        ak, sk = creds
        return await asyncio.to_thread(self._fetch_sync, ak, sk, sid, kw)

    def _fetch_sync(self, ak, sk, sector_id, keyword) -> dict | None:
        from bottleneck_hunter.data_provider import gangtise_client as gc
        # 主营业务包含瓶颈关键词：零日期参数、确定性过滤（indicatorList 不可空、expression 必填）
        cands = gc.screen(
            ak, sk, universe=[sector_id], expression=f"F1 contains '{keyword}'",
            indicator_list=[{"field": "F1", "indicatorCode": "pty_main_bus", "parameters": []}])
        return {"candidates": cands} if cands else None


def _to_ts_code(ticker: str) -> str:
    """A股 ticker → Tushare ts_code. '600000'→'600000.SH','000001'→'000001.SZ',北交所→.BJ;已带后缀则原样。"""
    t = ticker.strip().upper()
    if "." in t:
        return t
    if len(t) == 6 and t.isdigit():
        if t[0] == "6":
            return f"{t}.SH"
        if t[0] in ("4", "8") or t.startswith("920"):  # 北交所
            return f"{t}.BJ"
        return f"{t}.SZ"
    return ""


def build_providers() -> list:
    return [
        GangtiseProvider(),
        GangtiseNarrativeProvider(), GangtiseScreenProvider(),
        FMPProvider(), FinnhubProvider(), TushareProvider(), AkshareEarningsProvider(),
        AlphaVantageProvider(), TiingoProvider(), PolygonProvider(),
        YfinanceOptionsProvider(),
    ]


def _demo() -> None:
    """自检：Gangtise 利润表+一致预期 → 规范 dict 的纯映射（元→亿、自算毛利率、consensus 并入）。"""
    fin = {"report_date": "2025-03-31", "rows": [{
        "endDate": "2025-03-31",
        "opRev": 10_000_000_000,   # 100 亿
        "opCost": 6_000_000_000,   # 60 亿 → 毛利率 40%
        "netProfitAttrParent": 2_000_000_000,  # 20 亿
        "basicEPS": 1.5}]}
    forecast = {"forecasts": [{"forecastYear": "2025", "eps": 6.2, "pe": 18.5}]}
    d = _map_gangtise_financials(fin, forecast)
    assert d["data_source"] == "gangtise"
    assert d["revenue_yi"] == 100.0, d["revenue_yi"]
    assert d["net_profit_yi"] == 20.0, d["net_profit_yi"]
    assert d["gross_margin_pct"] == 40.0, d["gross_margin_pct"]
    assert d["consensus_eps"] == 6.2 and d["consensus_pe"] == 18.5, d
    assert d["report_date"] == "2025-03-31"
    assert d["currency"] == "CNY", d["currency"]
    # 美股：同刻度、币种 USD、一致预期留空（接口 A股-only）
    du = _map_gangtise_financials(fin, None, "us_stock")
    assert du["currency"] == "USD" and du["consensus_eps"] is None, du
    # 无一致预期 → consensus 留空，主体仍在
    d2 = _map_gangtise_financials(fin, None)
    assert d2["consensus_eps"] is None and d2["revenue_yi"] == 100.0
    # 回落 netProfit（无归母科目时）
    fin2 = {"report_date": "", "rows": [{"opRev": 1e8, "opCost": 6e7, "netProfit": 3e7}]}
    d3 = _map_gangtise_financials(fin2, None)
    assert d3["net_profit_yi"] == 0.3, d3["net_profit_yi"]
    # EDB 映射：identity 原值 + index100 变换 + change_pct=最新−前值
    raw = {
        "M00012461": {"latest": 3.5, "prev": 4.2, "date": "20260630"},   # 美CPI identity
        "M00000002": {"latest": 99.9, "prev": 100.3, "date": "20260731"},  # 中CPI index100
    }
    us = _map_gangtise_edb(raw, "us_stock")
    assert us["us_cpi_yoy"] == {"value": 3.5, "change_pct": -0.7,
                                "label": "美国CPI同比(%)", "as_of": "20260630"}, us
    cn = _map_gangtise_edb(raw, "a_stock")
    assert cn["cn_cpi_yoy"]["value"] == -0.1, cn   # 99.9-100
    assert cn["cn_cpi_yoy"]["change_pct"] == -0.4, cn  # (99.9-100)-(100.3-100)=-0.1-0.3
    assert "us_cpi_yoy" not in cn and "cn_cpi_yoy" not in us  # 市场隔离
    assert _map_gangtise_edb({}, "us_stock") == {}  # 空原始 → 空
    # 能力声明自洽（防 CAP_RESEARCH/CAP_KB/CAP_VALUATION 未导入导致运行时 NameError）
    p = GangtiseProvider()
    assert p.capabilities() == {CAP_FINANCIALS, CAP_MACRO_EDB, CAP_RESEARCH, CAP_KB, CAP_VALUATION}, p.capabilities()
    assert p.supports(CAP_RESEARCH, "us_stock") and p.supports(CAP_KB, "a_stock")
    assert not p.supports(CAP_RESEARCH, "hk_stock")
    # 估值分位仅 A股（valuation-analysis 端点非A股 120001）
    assert p.supports(CAP_VALUATION, "a_stock") and not p.supports(CAP_VALUATION, "us_stock")
    # 估值映射：原始 → 规范键（pe_ttm/pb_mrq/peg + *_percentile）
    _v = _map_gangtise_valuation({"peTtm": {"value": 17.2, "percentile": 17.24, "as_of": "2026-08-22"},
                                  "pbMrq": {"value": 8.1, "percentile": 10.49, "as_of": "2026-08-22"}})
    assert _v and _v["pe_ttm"] == 17.2 and _v["pe_ttm_percentile"] == 17.24 and _v["pb_mrq"] == 8.1, _v
    assert _v["data_source"] == "gangtise" and _v["as_of"] == "2026-08-22", _v
    assert _map_gangtise_valuation({}) is None and _map_gangtise_valuation(None) is None
    # 叙事 provider：A/US 都支持，只认 CAP_NARRATIVE
    n = GangtiseNarrativeProvider()
    assert n.capabilities() == {CAP_NARRATIVE}
    assert n.supports(CAP_NARRATIVE, "a_stock") and n.supports(CAP_NARRATIVE, "us_stock")
    assert not n.supports(CAP_NARRATIVE, "hk_stock") and not n.supports(CAP_SCREEN, "a_stock")
    # 选股 provider：仅 A股（§10 板块体系 A股-only）
    s = GangtiseScreenProvider()
    assert s.capabilities() == {CAP_SCREEN}
    assert s.supports(CAP_SCREEN, "a_stock")
    assert not s.supports(CAP_SCREEN, "us_stock") and not s.supports(CAP_NARRATIVE, "a_stock")
    print("providers gangtise demo: OK")


if __name__ == "__main__":
    _demo()
