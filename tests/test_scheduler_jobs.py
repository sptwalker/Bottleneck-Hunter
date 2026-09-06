"""Tests for scheduler.py — 决策自动调度任务。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist import scheduler


@pytest.fixture(autouse=True)
def _reset_scheduler_globals():
    """每个测试前重置 scheduler 模块全局变量。"""
    old_store = scheduler._wl_store
    old_budget = scheduler._budget
    old_sched = scheduler._scheduler
    yield
    scheduler._wl_store = old_store
    scheduler._budget = old_budget
    scheduler._scheduler = old_sched


def _setup_store(tickers=None):
    """设置 scheduler 模块级 store 和 budget。"""
    store = MagicMock()
    store.get_tickers_by_market.return_value = tickers or {}
    store.get_budget_limits.return_value = {"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0}
    store.get_daily_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    store.get_monthly_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    scheduler._wl_store = store
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    scheduler._budget = BudgetTracker(store)
    return store


class TestDrainSSE:
    @pytest.mark.asyncio
    async def test_consumes_all_events(self):
        """验证 _drain_sse 完整消费异步生成器。"""
        events = []

        async def gen():
            yield {"event": "step_start", "data": {"event": "step_start", "message": "开始"}}
            yield {"event": "step_done", "data": {"event": "step_done", "message": "完成"}}

        await scheduler._drain_sse(gen())

    @pytest.mark.asyncio
    async def test_logs_error_events(self):
        """验证 error 事件被记录为 warning。"""
        async def gen():
            yield {"event": "decision_error", "data": {"event": "decision_error", "error": "LLM 不可用"}}

        with patch.object(scheduler.logger, "warning") as mock_warn:
            await scheduler._drain_sse(gen())
            mock_warn.assert_called_once()

    @pytest.mark.asyncio
    async def test_logs_done_events(self):
        """验证 done 事件被记录为 info。"""
        async def gen():
            yield {"event": "decision_done", "data": {"event": "decision_done", "message": "L1 完成"}}

        with patch.object(scheduler.logger, "info") as mock_info:
            await scheduler._drain_sse(gen())
            mock_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_string_data(self):
        """验证 data 为 JSON 字符串时正确解析。"""
        import json
        async def gen():
            yield {"event": "done", "data": json.dumps({"event": "done", "message": "ok"})}

        with patch.object(scheduler.logger, "info") as mock_info:
            await scheduler._drain_sse(gen())
            mock_info.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_dict_data(self):
        """非 dict/string data 被跳过。"""
        async def gen():
            yield {"event": "x", "data": 42}

        await scheduler._drain_sse(gen())

    @pytest.mark.asyncio
    async def test_empty_generator(self):
        """空生成器不报错。"""
        async def gen():
            return
            yield  # noqa: make it a generator

        await scheduler._drain_sse(gen())


class TestJobDailyDecision:
    @pytest.mark.asyncio
    async def test_empty_watchlist_skips(self):
        """空观察池时跳过。"""
        _setup_store(tickers={})
        await scheduler.job_daily_decision(market="us_stock")

    @pytest.mark.asyncio
    async def test_no_store_returns(self):
        """store 为 None 时直接返回。"""
        scheduler._wl_store = None
        await scheduler.job_daily_decision()

    @pytest.mark.asyncio
    async def test_runs_decision(self):
        """有 ticker 时调用 run_daily_decision 并消费。"""
        _setup_store(tickers={"us_stock": ["AAPL", "MSFT"]})

        async def mock_gen(*args, **kwargs):
            yield {"event": "decision_done", "data": {"event": "decision_done", "message": "完成"}}

        with patch("bottleneck_hunter.watchlist.decision_engine.run_daily_decision", side_effect=mock_gen):
            await scheduler.job_daily_decision(market="us_stock")

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """异常不传播，仅记录。"""
        _setup_store(tickers={"us_stock": ["AAPL"]})

        with patch("bottleneck_hunter.watchlist.decision_engine.run_daily_decision", side_effect=RuntimeError("boom")):
            await scheduler.job_daily_decision(market="us_stock")

    @pytest.mark.asyncio
    async def test_had_fatal_raises_for_error_heartbeat(self):
        """had_fatal（无可用 LLM）必须让 job 抛错 → 心跳置 error → 守卫补跑。
        回归缺口1：旧代码在 per-user try 内 raise 被自己 except 吞掉，心跳恒 success，守卫机制形同虚设。"""
        store = MagicMock()
        store.get_tickers_by_market.return_value = {"us_stock": ["AAPL"]}
        with patch.object(scheduler, "_iter_users", return_value=iter([("u1", store, MagicMock())])), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_daily_decision", MagicMock(return_value=MagicMock())), \
             patch.object(scheduler, "_drain_sse", new=AsyncMock(return_value=True)), \
             patch.object(scheduler, "_oplog"):
            with pytest.raises(RuntimeError, match="无可用 LLM"):
                await scheduler.job_daily_decision(market="us_stock")

    @pytest.mark.asyncio
    async def test_had_fatal_preserves_user_isolation(self):
        """一个用户 had_fatal 不阻断其他用户：全部跑完后才在末尾统一抛错。"""
        s1, s2 = MagicMock(), MagicMock()
        s1.get_tickers_by_market.return_value = {"us_stock": ["AAPL"]}
        s2.get_tickers_by_market.return_value = {"us_stock": ["MSFT"]}
        drain = AsyncMock(side_effect=[True, False])  # 用户1 fatal，用户2 正常
        with patch.object(scheduler, "_iter_users",
                          return_value=iter([("u1", s1, MagicMock()), ("u2", s2, MagicMock())])), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_daily_decision", MagicMock(return_value=MagicMock())), \
             patch.object(scheduler, "_drain_sse", new=drain), \
             patch.object(scheduler, "_oplog"):
            with pytest.raises(RuntimeError):
                await scheduler.job_daily_decision(market="us_stock")
        assert drain.await_count == 2  # 两个用户都被处理（隔离保留），非首个 fatal 即中断

    @pytest.mark.asyncio
    async def test_all_ok_no_raise(self):
        """全部用户正常时不抛错（心跳 success）。"""
        store = MagicMock()
        store.get_tickers_by_market.return_value = {"us_stock": ["AAPL"]}
        with patch.object(scheduler, "_iter_users", return_value=iter([("u1", store, MagicMock())])), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_daily_decision", MagicMock(return_value=MagicMock())), \
             patch.object(scheduler, "_drain_sse", new=AsyncMock(return_value=False)), \
             patch.object(scheduler, "_oplog"):
            await scheduler.job_daily_decision(market="us_stock")  # 不应抛错


class TestJobCatalystScan:
    @pytest.mark.asyncio
    async def test_no_store_returns(self):
        scheduler._wl_store = None
        await scheduler.job_catalyst_scan()

    @pytest.mark.asyncio
    async def test_runs_both_functions(self):
        """验证同时调用 check_catalyst_expiry 和 detect_catalysts。"""
        store = _setup_store()

        async def mock_expiry(*args, **kwargs):
            yield {"event": "catalyst_expired", "data": {"event": "catalyst_expired", "count": 1}}

        async def mock_detect(*args, **kwargs):
            yield {"event": "catalyst_scan_done", "data": {"event": "catalyst_scan_done", "message": "完成"}}

        with patch("bottleneck_hunter.watchlist.catalyst_monitor.check_catalyst_expiry", side_effect=mock_expiry), \
             patch("bottleneck_hunter.watchlist.catalyst_monitor.detect_catalysts", side_effect=mock_detect):
            await scheduler.job_catalyst_scan()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        store = _setup_store()
        with patch("bottleneck_hunter.watchlist.catalyst_monitor.check_catalyst_expiry", side_effect=RuntimeError("err")):
            await scheduler.job_catalyst_scan()


class TestJobWeeklyStrategy:
    @pytest.mark.asyncio
    async def test_no_store_returns(self):
        scheduler._wl_store = None
        await scheduler.job_weekly_strategy()

    @pytest.mark.asyncio
    async def test_runs_both_l1_l2(self):
        """验证同时调用 run_macro_strategy 和 run_strategic_plan。"""
        store = _setup_store()
        calls = []

        async def mock_macro(*args, **kwargs):
            calls.append("macro")
            yield {"event": "decision_done", "data": {"event": "decision_done", "message": "L1"}}

        async def mock_strategic(*args, **kwargs):
            calls.append("strategic")
            yield {"event": "decision_done", "data": {"event": "decision_done", "message": "L2"}}

        with patch("bottleneck_hunter.watchlist.decision_engine.run_macro_strategy", side_effect=mock_macro), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_strategic_plan", side_effect=mock_strategic):
            await scheduler.job_weekly_strategy()

        assert "macro" in calls
        assert "strategic" in calls

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        store = _setup_store()
        with patch("bottleneck_hunter.watchlist.decision_engine.run_macro_strategy", side_effect=RuntimeError("err")):
            await scheduler.job_weekly_strategy()

    @pytest.mark.asyncio
    async def test_had_fatal_raises_for_error_heartbeat(self):
        """L1/L2 无可用 LLM 必须让 job 抛错 → 心跳 error → 守卫补跑（回归缺口1）。"""
        store = MagicMock()
        with patch.object(scheduler, "_iter_users", return_value=iter([("u1", store, MagicMock())])), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_macro_strategy", MagicMock(return_value=MagicMock())), \
             patch("bottleneck_hunter.watchlist.decision_engine.run_strategic_plan", MagicMock(return_value=MagicMock())), \
             patch.object(scheduler, "_drain_sse", new=AsyncMock(side_effect=[True, False])), \
             patch.object(scheduler, "_oplog"):
            with pytest.raises(RuntimeError, match="无可用 LLM"):
                await scheduler.job_weekly_strategy()


class TestJobAutoReview:
    @pytest.mark.asyncio
    async def test_no_store_returns(self):
        scheduler._wl_store = None
        await scheduler.job_auto_review()

    @pytest.mark.asyncio
    async def test_runs_batch_review(self):
        store = _setup_store()

        async def mock_review(*args, **kwargs):
            yield {"event": "batch_review_done", "data": {"event": "batch_review_done", "message": "完成"}}

        with patch("bottleneck_hunter.watchlist.trade_reviewer.run_batch_review", side_effect=mock_review):
            await scheduler.job_auto_review()

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        store = _setup_store()
        with patch("bottleneck_hunter.watchlist.trade_reviewer.run_batch_review", side_effect=RuntimeError("err")):
            await scheduler.job_auto_review()


class TestGetJobStatuses:
    def test_no_scheduler(self):
        scheduler._scheduler = None
        assert scheduler.get_job_statuses() == []

    def test_returns_job_list(self):
        mock_job = MagicMock()
        mock_job.id = "us_daily_decision"
        mock_job.name = "Daily decision"
        mock_job.next_run_time = None

        mock_sched = MagicMock()
        mock_sched.get_jobs.return_value = [mock_job]
        scheduler._scheduler = mock_sched

        result = scheduler.get_job_statuses()
        assert len(result) == 1
        assert result[0]["id"] == "us_daily_decision"
        assert result[0]["name"] == "Daily decision"
        assert result[0]["next_run_at"] is None
