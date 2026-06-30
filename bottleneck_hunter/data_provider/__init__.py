"""统一数据源抽象层。

使用 get_fetcher_manager() 获取全局 FetcherManager 单例。
"""

from __future__ import annotations

import logging

from bottleneck_hunter.data_provider.manager import FetcherManager

logger = logging.getLogger(__name__)

_manager: FetcherManager | None = None


def get_fetcher_manager() -> FetcherManager:
    """获取全局 FetcherManager 单例（延迟初始化）。"""
    global _manager
    if _manager is None:
        _manager = _create_manager()
    return _manager


def _create_manager() -> FetcherManager:
    manager = FetcherManager()

    # A股：efinance (priority=0) > akshare (1) > pytdx (2)
    try:
        from bottleneck_hunter.data_provider.fetchers.efinance_fetcher import EfinanceFetcher
        manager.register(EfinanceFetcher())
    except ImportError:
        logger.info("efinance 未安装，跳过")

    try:
        from bottleneck_hunter.data_provider.fetchers.akshare_fetcher import AkshareFetcher
        manager.register(AkshareFetcher())
    except ImportError:
        logger.info("akshare 未安装，跳过")

    try:
        from bottleneck_hunter.data_provider.fetchers.pytdx_fetcher import PytdxFetcher
        manager.register(PytdxFetcher())
    except ImportError:
        logger.info("pytdx 未安装，跳过")

    # 美股：yfinance (priority=0) > finnhub (2)
    try:
        from bottleneck_hunter.data_provider.fetchers.yfinance_fetcher import YfinanceFetcher
        manager.register(YfinanceFetcher())
    except ImportError:
        logger.info("yfinance 未安装，跳过")

    try:
        from bottleneck_hunter.data_provider.fetchers.finnhub_fetcher import FinnhubFetcher
        manager.register(FinnhubFetcher())
    except ImportError:
        logger.info("finnhub 未安装，跳过")

    return manager
