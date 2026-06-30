"""FetcherManager — 自动降级、优先级路由、熔断器。"""

from __future__ import annotations

import logging
import time

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote

logger = logging.getLogger(__name__)

CIRCUIT_BREAK_THRESHOLD = 5
CIRCUIT_BREAK_COOLDOWN = 60  # 秒

# 不计入熔断的异常（通常是参数错误，非数据源故障）
_NON_RETRIABLE = (ValueError, KeyError, TypeError)


class _FetcherState:
    """单个 fetcher 的运行时状态（失败计数、熔断时间）。"""

    def __init__(self, fetcher: BaseFetcher):
        self.fetcher = fetcher
        self.fail_count: int = 0
        self.last_fail_time: float = 0.0
        self.total_calls: int = 0
        self.total_failures: int = 0

    @property
    def is_circuit_open(self) -> bool:
        if self.fail_count < CIRCUIT_BREAK_THRESHOLD:
            return False
        elapsed = time.time() - self.last_fail_time
        if elapsed > CIRCUIT_BREAK_COOLDOWN:
            logger.info("熔断恢复: %s (冷却 %.0fs 已过)", self.fetcher.name, elapsed)
            self.fail_count = 0
            return False
        return True

    def record_success(self):
        if self.fail_count > 0:
            logger.info("数据源恢复: %s (之前连续失败 %d 次)", self.fetcher.name, self.fail_count)
        self.fail_count = 0
        self.total_calls += 1

    def record_failure(self):
        self.fail_count += 1
        self.last_fail_time = time.time()
        self.total_calls += 1
        self.total_failures += 1
        if self.fail_count == CIRCUIT_BREAK_THRESHOLD:
            logger.warning("触发熔断: %s (连续失败 %d 次，冷却 %ds)",
                           self.fetcher.name, self.fail_count, CIRCUIT_BREAK_COOLDOWN)


class FetcherManager:
    """按优先级管理多个数据源，自动降级和熔断。"""

    def __init__(self, fetchers: list[BaseFetcher] | None = None):
        self._states: dict[str, _FetcherState] = {}
        if fetchers:
            for f in fetchers:
                self.register(f)

    def register(self, fetcher: BaseFetcher):
        self._states[fetcher.name] = _FetcherState(fetcher)
        logger.info("注册数据源: %s (优先级=%d, 市场=%s)",
                     fetcher.name, fetcher.priority, fetcher.supported_markets)

    def _get_fetchers_for(self, market: str) -> list[_FetcherState]:
        """按优先级排序，返回支持指定市场且未熔断的 fetcher。"""
        candidates = [
            s for s in self._states.values()
            if s.fetcher.supports(market) and not s.is_circuit_open
        ]
        candidates.sort(key=lambda s: s.fetcher.priority)
        return candidates

    async def fetch_daily(self, ticker: str, market: str, days: int = 180) -> pd.DataFrame | None:
        """按优先级逐个尝试获取日K线，成功即返回。"""
        candidates = self._get_fetchers_for(market)
        if not candidates:
            logger.error("无可用数据源: market=%s, ticker=%s", market, ticker)
            return None

        last_err = None
        for state in candidates:
            try:
                df = await state.fetcher.fetch_daily(ticker, days)
                if df is not None and not df.empty:
                    state.record_success()
                    return df
                logger.debug("%s 返回空数据: %s", state.fetcher.name, ticker)
            except _NON_RETRIABLE as e:
                logger.debug("%s 参数错误 (%s): %s — 跳过不计入熔断",
                             state.fetcher.name, ticker, e)
            except Exception as e:
                state.record_failure()
                last_err = e
                logger.warning("%s 获取日K失败 (%s): %s — 尝试下一个数据源",
                               state.fetcher.name, ticker, e)

        if last_err:
            logger.error("所有数据源均失败: %s (market=%s), 最后错误: %s",
                         ticker, market, last_err)
        return None

    async def fetch_realtime(self, ticker: str, market: str) -> StandardQuote | None:
        """按优先级逐个尝试获取实时行情。"""
        candidates = self._get_fetchers_for(market)
        if not candidates:
            logger.error("无可用实时数据源: market=%s, ticker=%s", market, ticker)
            return None

        last_err = None
        for state in candidates:
            try:
                quote = await state.fetcher.fetch_realtime(ticker)
                if quote is not None:
                    state.record_success()
                    return quote
            except _NON_RETRIABLE as e:
                logger.debug("%s 实时行情参数错误 (%s): %s", state.fetcher.name, ticker, e)
            except Exception as e:
                state.record_failure()
                last_err = e
                logger.warning("%s 获取实时行情失败 (%s): %s",
                               state.fetcher.name, ticker, e)

        if last_err:
            logger.error("所有实时数据源均失败: %s (market=%s)", ticker, market)
        return None

    def get_status(self) -> list[dict]:
        """返回所有数据源的状态信息。"""
        result = []
        for name, state in self._states.items():
            result.append({
                "name": name,
                "priority": state.fetcher.priority,
                "markets": list(state.fetcher.supported_markets),
                "circuit_open": state.is_circuit_open,
                "fail_count": state.fail_count,
                "total_calls": state.total_calls,
                "total_failures": state.total_failures,
            })
        return sorted(result, key=lambda x: x["priority"])
