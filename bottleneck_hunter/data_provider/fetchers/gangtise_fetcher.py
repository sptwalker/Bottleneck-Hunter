"""Gangtise 行情源包装 — 美股 + A股 日K线和实时快照（FetcherManager 路径最高优先级）。

master plan 的 `GangtiseProvider` 走 **hub 路径**（财务/宏观/研报/KB/估值）；本 fetcher 补 hub 未覆盖的
**第二条 dispatch 路径**——`FetcherManager` 的 CAP_QUOTE/CAP_DAILY 行情。

priority=-1（严格最高档，`order()` 升序恒排最前）：Gangtise 行情走 admin 共享 key、境内可达、官方口径、
免费不限流，优先于 yfinance/efinance；连续 5 次失败熔断 60s 自动降级到既有免费源，兜底链完好。

凭据缺失（未授权 / 无 current_user / 异常）→ 返回 None（触发 fallback），**绝不 raise**（否则拖垮降级链）；
接口/网络错误 → 抛 `GangtiseError`（非 `_NON_RETRIABLE` → 计入熔断）。fetcher 是全局单例，**绝不缓存 Key**。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote

logger = logging.getLogger(__name__)


class GangtiseFetcher(BaseFetcher):
    name = "gangtise"
    priority = -1  # 负值=严格最高档：order() 按 (priority, recent_load) 升序，-1<0 恒先于 efinance/yfinance
    supported_markets = {"a_stock", "us_stock"}

    @staticmethod
    def _infer_market(ticker: str) -> str:
        """manager 不透传 market（fetch_daily(ticker, days) 无 market 参数），从 ticker 推断。

        本系统 A股码恒为 6 位纯数字（可带 .SH/.SZ/.BJ 后缀），美股码恒为字母——与
        `gangtise_client._sec_code`/`_resolve_gts_code` 的 market 分支同源。manager 已按真实
        market 路由到本 fetcher，推断只用于选 gtsCode 解析路径（A股直通 vs 美股 securities/search）。
        注：A股量能单位归一由 manager 用**真实 market** 调 `clean_ohlc`，不依赖此推断。
        """
        base = ticker.strip().upper().split(".")[0]
        return "a_stock" if (base.isdigit() and len(base) == 6) else "us_stock"

    def _creds(self):
        """实时解析 admin 授权凭据（受控显式共享，见 data_source_catalog）。未授权/无上下文/异常 → None。"""
        try:
            from bottleneck_hunter.data_provider.data_source_catalog import resolve_gangtise_credentials
            return resolve_gangtise_credentials()
        except Exception:  # noqa: BLE001
            return None

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        creds = self._creds()
        if not creds:
            return None  # 无凭据 → 交 manager 降级到 yfinance/efinance（绝不 raise）
        ak, sk = creds
        market = self._infer_market(ticker)

        def _fetch():
            from bottleneck_hunter.data_provider import gangtise_client as gc
            rows = gc.fetch_ohlcv_daily(ak, sk, ticker, market, days)
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
            if days < len(df):
                df = df.tail(days).reset_index(drop=True)
            return df

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        creds = self._creds()
        if not creds:
            return None
        ak, sk = creds
        market = self._infer_market(ticker)

        def _fetch():
            from bottleneck_hunter.data_provider import gangtise_client as gc
            q = gc.fetch_realtime_quote(ak, sk, ticker, market)
            if not q or not q.get("price"):
                return None
            return StandardQuote(
                ticker=ticker,
                price=float(q["price"]),
                change_pct=float(q.get("change_pct") or 0.0),
                volume=int(q.get("volume") or 0),
                amount=float(q.get("amount") or 0.0),
                source="gangtise",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        return await asyncio.to_thread(_fetch)

    async def health_check(self) -> bool:
        return self._creds() is not None
