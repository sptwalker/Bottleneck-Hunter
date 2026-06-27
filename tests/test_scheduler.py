"""scheduler 单元测试。

覆盖调度器初始化/关闭、价格更新按市场过滤、每日扫描的差异化管道调用。
APScheduler 为可选依赖，未安装时自动跳过。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore

# APScheduler 可能未安装 — 标记跳过条件
try:
    import apscheduler
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False

skip_no_apscheduler = pytest.mark.skipif(
    not _HAS_APSCHEDULER,
    reason="apscheduler 未安装",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """创建临时 WatchlistStore 并添加 2 只美股 + 1 只 A 股。"""
    s = WatchlistStore(tmp_path / "test.db")
    s.add({"ticker": "AAPL", "company_name": "Apple", "market": "us_stock", "tier": "focus"})
    s.add({"ticker": "MSFT", "company_name": "Microsoft", "market": "us_stock", "tier": "focus"})
    s.add({"ticker": "SH600519", "company_name": "贵州茅台", "market": "a_stock", "tier": "track"})
    return s


@pytest.fixture(autouse=True)
def _reset_scheduler_globals():
    """每个测试前后重置 scheduler 模块的全局变量，避免测试间污染。"""
    import bottleneck_hunter.watchlist.scheduler as sched_mod
    sched_mod._scheduler = None
    sched_mod._wl_store = None
    sched_mod._budget = None
    yield
    sched_mod._scheduler = None
    sched_mod._wl_store = None
    sched_mod._budget = None


# ---------------------------------------------------------------------------
# TestSchedulerInit — 调度器初始化与关闭
# ---------------------------------------------------------------------------

class TestSchedulerInit:
    """测试 init_scheduler / shutdown_scheduler / get_job_statuses。"""

    @skip_no_apscheduler
    async def test_init_creates_14_jobs(self, store):
        """init_scheduler 后应注册 16 个定时任务（6 数据 + 1 institutional + 1 macro + 8 决策）。"""
        from bottleneck_hunter.watchlist.scheduler import (
            get_job_statuses,
            init_scheduler,
        )

        scheduler = init_scheduler(store)
        assert scheduler is not None

        # 需要 start(paused=True) 才能让 next_run_time 可访问
        # （APScheduler 在 start 前不分配 next_run_time slot）
        scheduler.start(paused=True)
        try:
            jobs = get_job_statuses()
            assert len(jobs) == 16

            # 验证所有预期 job id 都存在
            expected_ids = {
                "us_price_premarket",
                "us_price_postmarket",
                "us_daily_scan",
                "cn_price_premarket",
                "cn_price_postmarket",
                "cn_daily_scan",
                "us_daily_decision",
                "us_catalyst_scan",
                "us_weekly_strategy",
                "us_auto_review",
                "cn_daily_decision",
                "cn_catalyst_scan",
                "cn_weekly_strategy",
                "cn_auto_review",
                "macro_update",
                "us_institutional_update",
            }
            actual_ids = {j["id"] for j in jobs}
            assert actual_ids == expected_ids
        finally:
            scheduler.shutdown(wait=False)

    @skip_no_apscheduler
    async def test_shutdown_cleans_up(self, store):
        """shutdown_scheduler 后 get_job_statuses 应返回空列表。"""
        from bottleneck_hunter.watchlist.scheduler import (
            get_job_statuses,
            init_scheduler,
            shutdown_scheduler,
        )

        scheduler = init_scheduler(store)
        scheduler.start(paused=True)
        # 关闭之前确认有 job
        assert len(get_job_statuses()) == 16

        shutdown_scheduler()
        # 关闭之后应为空
        assert get_job_statuses() == []


# ---------------------------------------------------------------------------
# TestJobDispatching — 任务调度的市场过滤与管道调用
# ---------------------------------------------------------------------------

class TestJobDispatching:
    """测试 job_price_update / job_daily_scan 的市场过滤逻辑。"""

    async def test_price_update_filters_by_market(self, store):
        """job_price_update(market="us_stock") 只传美股 ticker。"""
        from bottleneck_hunter.watchlist.scheduler import (
            init_scheduler,
            job_price_update,
        )

        init_scheduler(store)

        # scheduler 内部 lazy import: from ...price_pipeline import fetch_price_batch
        # patch 被导入模块的函数，让 lazy import 拿到 mock
        with patch(
            "bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": "ok", "MSFT": "ok"},
        ) as mock_fetch:
            results = await job_price_update(market="us_stock")

        # 验证 fetch_price_batch 被调用时的 tickers 参数只含美股
        mock_fetch.assert_called_once()
        call_args = mock_fetch.call_args
        tickers_arg = call_args[0][0]  # 第一个位置参数
        assert "AAPL" in tickers_arg
        assert "MSFT" in tickers_arg
        assert "SH600519" not in tickers_arg

    async def test_daily_scan_astock_skips_sec(self, store):
        """job_daily_scan(market="a_stock") 不调用 SEC 和 Options 管道。"""
        from bottleneck_hunter.watchlist.scheduler import (
            init_scheduler,
            job_daily_scan,
        )

        init_scheduler(store)

        with patch(
            "bottleneck_hunter.watchlist.news_pipeline.fetch_news_batch",
            new_callable=AsyncMock,
            return_value={"SH600519": 3},
        ) as mock_news, patch(
            "bottleneck_hunter.watchlist.sec_pipeline.fetch_sec_batch",
            new_callable=AsyncMock,
        ) as mock_sec, patch(
            "bottleneck_hunter.watchlist.options_pipeline.fetch_options_batch",
            new_callable=AsyncMock,
        ) as mock_options:
            results = await job_daily_scan(market="a_stock")

        # A 股不调用 SEC 和 Options
        mock_sec.assert_not_called()
        mock_options.assert_not_called()

    async def test_daily_scan_us_includes_all(self, store):
        """job_daily_scan(market="us_stock") 调用 news + sec + options。"""
        from bottleneck_hunter.watchlist.scheduler import (
            init_scheduler,
            job_daily_scan,
        )

        init_scheduler(store)

        with patch(
            "bottleneck_hunter.watchlist.news_pipeline.fetch_news_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": 5, "MSFT": 3},
        ) as mock_news, patch(
            "bottleneck_hunter.watchlist.sec_pipeline.fetch_sec_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": {"filings": 2}, "MSFT": {"filings": 1}},
        ) as mock_sec, patch(
            "bottleneck_hunter.watchlist.options_pipeline.fetch_options_batch",
            new_callable=AsyncMock,
            return_value={"AAPL": "ok", "MSFT": "ok"},
        ) as mock_options:
            results = await job_daily_scan(market="us_stock")

        # 美股应同时调用三个管道
        mock_news.assert_called_once()
        mock_sec.assert_called_once()
        mock_options.assert_called_once()

        # 验证结果包含三个管道的数据
        assert "news" in results
        assert "sec" in results
        assert "options" in results
