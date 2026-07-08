"""会前外网数据预取器 — 为圆桌会议提供最新市场数据。

在会议开始前批量拉取所有入围企业的最新行情、新闻、分析师评级，
编入背景材料让所有参会 AI 共享，避免依赖不稳定的 LLM tool calling。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

_SEMAPHORE = asyncio.Semaphore(6)


def _safe_float(val: Any, scale: float = 1.0) -> Optional[float]:
    if val is None:
        return None
    try:
        v = float(str(val).replace(",", "").replace("%", ""))
        return round(v * scale, 4)
    except (ValueError, TypeError):
        return None


class MeetingDataFetcher:
    """会前数据预取器。"""

    async def fetch_all(
        self, ticker_markets: dict[str, str]
    ) -> dict[str, dict]:
        """按每个 ticker 自己的 market 逐票取数（避免混合分析下 A股票被当美股）。"""
        items = list(ticker_markets.items())
        tasks = [self._fetch_one(t, m) for t, m in items]
        done = await asyncio.gather(*tasks, return_exceptions=True)
        results = {}
        for (ticker, _m), result in zip(items, done):
            if isinstance(result, Exception):
                logger.warning(f"预取数据失败 ({ticker}): {result}")
                results[ticker] = {}
            else:
                results[ticker] = result
        return results

    async def _fetch_one(self, ticker: str, market: str) -> dict:
        async with _SEMAPHORE:
            data: dict[str, Any] = {}
            if market == "a_stock":
                code = ticker.split(".")[0].strip()
                if code.isdigit() and len(code) == 6:
                    data["price"] = await asyncio.to_thread(self._fetch_a_price, code)
                    data["news"] = await asyncio.to_thread(self._fetch_a_news, code)
                    data["analyst"] = await asyncio.to_thread(self._fetch_a_analyst, code)
            else:
                tk = ticker.replace(".", "-").strip()  # 美股类别股 BRK.B→BRK-B，勿去后缀
                if tk:
                    data["price"] = await asyncio.to_thread(self._fetch_us_price, tk)
                    data["news"] = await asyncio.to_thread(self._fetch_us_news, tk)
                    data["analyst"] = await asyncio.to_thread(self._fetch_us_analyst, tk)
            return data

    # ── A 股 ──────────────────────────────────────────

    @staticmethod
    def _fetch_a_price(code: str) -> dict:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            if df is None or df.empty:
                return {}
            row = df[df["代码"] == code]
            if row.empty:
                return {}
            r = row.iloc[0]
            return {
                "latest": _safe_float(r.get("最新价")),
                "change_pct": _safe_float(r.get("涨跌幅")),
                "volume_yi": _safe_float(r.get("成交额"), 1e-8),
                "turnover_pct": _safe_float(r.get("换手率")),
            }
        except Exception as e:
            logger.debug(f"A股行情获取失败 ({code}): {e}")
            return {}

    @staticmethod
    def _fetch_a_news(code: str) -> list[str]:
        try:
            import akshare as ak
            df = ak.stock_news_em(symbol=code)
            if df is None or df.empty:
                return []
            titles = df["新闻标题"].tolist() if "新闻标题" in df.columns else []
            return [str(t) for t in titles[:5]]
        except Exception as e:
            logger.debug(f"A股新闻获取失败 ({code}): {e}")
            return []

    @staticmethod
    def _fetch_a_analyst(code: str) -> dict:
        try:
            import akshare as ak
            df = ak.stock_research_report_em(symbol=code)
            if df is None or df.empty:
                return {}
            cols = df.columns.tolist()
            date_col = [c for c in cols if "日期" in c]
            inst_col = [c for c in cols if "机构" in c]
            rating_col = [c for c in cols if "评级" in c]

            result: dict[str, Any] = {"total_reports": len(df)}

            if date_col and inst_col:
                cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                df["_d"] = df[date_col[0]].astype(str)
                recent = df[df["_d"] >= cutoff]
                result["recent_institutions"] = int(recent[inst_col[0]].nunique()) if not recent.empty else 0

            if rating_col:
                result["latest_rating"] = str(df.iloc[0][rating_col[0]])

            return result
        except Exception as e:
            logger.debug(f"A股研报获取失败 ({code}): {e}")
            return {}

    # ── 美股 ──────────────────────────────────────────

    @staticmethod
    def _fetch_us_price(ticker: str) -> dict:
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info or {}
            return {
                "latest": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
                "change_pct": _safe_float(info.get("regularMarketChangePercent")),
                "volume": info.get("regularMarketVolume"),
                "market_cap_b": _safe_float(info.get("marketCap"), 1e-9),
            }
        except Exception as e:
            logger.debug(f"美股行情获取失败 ({ticker}): {e}")
            return {}

    @staticmethod
    def _fetch_us_news(ticker: str) -> list[str]:
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            news = stock.news or []
            return [item.get("title", "") for item in news[:5] if item.get("title")]
        except Exception as e:
            logger.debug(f"美股新闻获取失败 ({ticker}): {e}")
            return []

    @staticmethod
    def _fetch_us_analyst(ticker: str) -> dict:
        try:
            import yfinance as yf
            stock = yf.Ticker(ticker)
            info = stock.info or {}
            result: dict[str, Any] = {}
            rec_key = info.get("recommendationKey")
            if rec_key:
                result["rating"] = rec_key
            n = info.get("numberOfAnalystOpinions")
            if n is not None:
                result["analyst_count"] = int(n)
            tp = info.get("targetMeanPrice")
            if tp is not None:
                result["target_price"] = _safe_float(tp)
            tp_hi = info.get("targetHighPrice")
            tp_lo = info.get("targetLowPrice")
            if tp_hi is not None:
                result["target_high"] = _safe_float(tp_hi)
            if tp_lo is not None:
                result["target_low"] = _safe_float(tp_lo)
            return result
        except Exception as e:
            logger.debug(f"美股分析师获取失败 ({ticker}): {e}")
            return {}

    # ── 格式化输出 ──────────────────────────────────────

    def format_for_briefing(
        self, all_data: dict[str, dict], name_map: dict[str, str] | None = None
    ) -> str:
        name_map = name_map or {}
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"\n# 最新市场数据（{today}）\n"]

        for ticker, data in all_data.items():
            if not data:
                continue
            name = name_map.get(ticker, ticker)
            lines.append(f"## {name} ({ticker})")

            price = data.get("price", {})
            if price:
                parts = []
                if price.get("latest") is not None:
                    parts.append(f"最新价: {price['latest']}")
                if price.get("change_pct") is not None:
                    parts.append(f"涨跌幅: {price['change_pct']:+.2f}%")
                if price.get("volume_yi") is not None:
                    parts.append(f"成交额: {price['volume_yi']:.1f}亿")
                elif price.get("volume") is not None:
                    parts.append(f"成交量: {price['volume']:,}")
                if parts:
                    lines.append(f"- 行情: {' | '.join(parts)}")

            news = data.get("news", [])
            if news:
                lines.append(f"- 近期新闻:")
                for title in news[:3]:
                    lines.append(f"  - {title}")

            analyst = data.get("analyst", {})
            if analyst:
                parts = []
                if analyst.get("rating"):
                    parts.append(f"评级: {analyst['rating']}")
                if analyst.get("analyst_count") is not None:
                    parts.append(f"覆盖分析师: {analyst['analyst_count']}")
                if analyst.get("recent_institutions") is not None:
                    parts.append(f"近3月覆盖机构: {analyst['recent_institutions']}")
                if analyst.get("target_price") is not None:
                    parts.append(f"目标价均值: {analyst['target_price']}")
                if parts:
                    lines.append(f"- 分析师: {' | '.join(parts)}")

            lines.append("")

        return "\n".join(lines) if len(lines) > 2 else ""
