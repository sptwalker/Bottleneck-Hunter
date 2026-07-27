"""tests 级共享夹具。"""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _stub_cn_market_news():
    """全局兜掉财联社快讯抓取（akshare stock_info_global_cls 真网络 + 10× retry-sleep）。

    fetch_market_news 在 RSS 抓不到时会兜底 _fetch_cn_market_news（真网络），离线/被墙环境下
    任何触达该兜底的测试（run_macro_* 等）会挂在 akshare 的重试 sleep 上，使明文 `pytest tests/` 卡死。
    需验证该抓取本身的测试自行 patch 覆盖即可（现无此类测试；astock 解析测试走 _fetch_astock_news，不受影响）。
    """
    with patch("bottleneck_hunter.watchlist.news_pipeline._fetch_cn_market_news", return_value=[]):
        yield
