"""统一数据源抽象层。

使用 get_fetcher_manager() 获取全局 FetcherManager 单例。
"""

from __future__ import annotations

import importlib.util
import logging

from bottleneck_hunter.data_provider.manager import FetcherManager

logger = logging.getLogger(__name__)


def _installed(module: str) -> bool:
    """底层库是否已安装。fetcher 都是惰性 import，ImportError 不会在注册期抛出，
    故在此显式探测——避免把不可用源注册进降级链、白占优先级槽并触发假熔断。"""
    return importlib.util.find_spec(module) is not None

_manager: FetcherManager | None = None


def get_fetcher_manager() -> FetcherManager:
    """获取全局 FetcherManager 单例（延迟初始化）。"""
    global _manager
    if _manager is None:
        _manager = _create_manager()
    return _manager


def _create_manager() -> FetcherManager:
    manager = FetcherManager()

    # Gangtise 行情（priority=-1，严格最高档，A股+美股统一）：admin 共享 key、境内可达、官方口径、
    # 免费不限流，优先于 efinance/yfinance；无凭据时 fetcher 自返 None（等效未注册，但授权后即生效）。
    # 无 _installed() 门：requests-only 无包依赖。故障熔断 60s 自动降级到下方免费源。
    try:
        from bottleneck_hunter.data_provider.fetchers.gangtise_fetcher import GangtiseFetcher
        manager.register(GangtiseFetcher())
    except ImportError:
        logger.info("gangtise fetcher 导入失败，跳过行情最高优先层")

    # A股：efinance (priority=0) > akshare (1) > pytdx (2)
    if _installed("efinance"):
        from bottleneck_hunter.data_provider.fetchers.efinance_fetcher import EfinanceFetcher
        manager.register(EfinanceFetcher())
    else:
        logger.info("efinance 未安装，跳过")

    try:
        from bottleneck_hunter.data_provider.fetchers.akshare_fetcher import AkshareFetcher
        manager.register(AkshareFetcher())
    except ImportError:
        logger.info("akshare 未安装，跳过")

    if _installed("pytdx"):
        from bottleneck_hunter.data_provider.fetchers.pytdx_fetcher import PytdxFetcher
        manager.register(PytdxFetcher())
    else:
        logger.info("pytdx 未安装，跳过")

    # A股可靠兜底：baostock (priority=3)，走独立服务器，不依赖东方财富接口
    try:
        from bottleneck_hunter.data_provider.fetchers.baostock_fetcher import BaostockFetcher
        manager.register(BaostockFetcher())
    except ImportError:
        logger.info("baostock 未安装，跳过")

    # 美股：yfinance (priority=0) > akshare_us (1, 国内可达兜底) > finnhub (2, 需密钥) > alphavantage (3, 应急)
    try:
        from bottleneck_hunter.data_provider.fetchers.yfinance_fetcher import YfinanceFetcher
        manager.register(YfinanceFetcher())
    except ImportError:
        logger.info("yfinance 未安装，跳过")

    # 美股国内兜底：yfinance 在境内数据中心常被 Yahoo 限流(Too Many Requests)，
    # 用新浪美股(akshare)兜底，免密钥、国内可达。
    try:
        from bottleneck_hunter.data_provider.fetchers.akshare_us_fetcher import AkshareUsFetcher
        manager.register(AkshareUsFetcher())
    except ImportError:
        logger.info("akshare 未安装，跳过美股兜底")

    # finnhub 惰性 import finnhub 包（try/except ImportError 拦不住，同 efinance/pytdx）→ 未装则显式跳过，
    # 否则每次美股实时取数都白撞 "No module named 'finnhub'"、拖慢降级、误报"所有实时数据源均失败"。
    if _installed("finnhub"):
        from bottleneck_hunter.data_provider.fetchers.finnhub_fetcher import FinnhubFetcher
        manager.register(FinnhubFetcher())
    else:
        logger.info("finnhub 未安装，跳过")

    # 美股最后一层应急兜底：alphavantage (priority=3)。走独立基础设施(www.alphavantage.co)，
    # 与 Yahoo/新浪/finnhub 互不相关，前三层同时熔断时接管。免费档极紧(~25/日)，
    # 复用数据源目录已有 Key、只用 requests(无需 _installed 探测)，scheduler 额度阀已防打爆。
    try:
        from bottleneck_hunter.data_provider.fetchers.alphavantage_fetcher import AlphaVantageFetcher
        manager.register(AlphaVantageFetcher())
    except ImportError:
        logger.info("alphavantage fetcher 导入失败，跳过美股应急兜底")

    return manager
