"""DataHub CapabilityProvider 实现 — 付费源封装（FMP/Tushare）。

用 resolve_data_source_key 取 key（DB→env），映射到 earnings 规范 dict（对齐 earnings_reports 表列，
save_earnings 可直存）。requests 同步调用放 asyncio.to_thread。
"""

from __future__ import annotations

import asyncio
import logging

import requests

from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
from bottleneck_hunter.data_provider.hub import CAP_EARNINGS

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_UA = {"User-Agent": "BottleneckHunter/1.0"}


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
    """Financial Modeling Prep — 美股 earnings（含一致预期）。"""
    name = "fmp"
    priority = 0

    def capabilities(self) -> set[str]:
        return {CAP_EARNINGS}

    def markets(self) -> set[str]:
        return {"us_stock"}

    def supports(self, capability: str, market: str) -> bool:
        return capability in self.capabilities() and market in self.markets()

    async def fetch(self, capability, ticker, market, user_id="") -> dict | None:
        if capability != CAP_EARNINGS:
            return None
        key = resolve_data_source_key("fmp", user_id)
        if not key:
            return None
        return await asyncio.to_thread(self._fetch_earnings_sync, ticker, key)

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


def _to_ts_code(ticker: str) -> str:
    """A股 ticker → Tushare ts_code。'600000'→'600000.SH'，'000001'→'000001.SZ'；已带后缀则原样。"""
    t = ticker.strip().upper()
    if "." in t:
        return t
    if len(t) == 6 and t.isdigit():
        return f"{t}.SH" if t[0] == "6" else f"{t}.SZ"
    return ""


def build_providers() -> list:
    return [FMPProvider(), TushareProvider()]
