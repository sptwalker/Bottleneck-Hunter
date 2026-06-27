"""Tests for Phase 12A — Semaphore 延迟初始化。"""

import asyncio
import importlib

import pytest


PIPELINE_MODULES = [
    "bottleneck_hunter.watchlist.price_pipeline",
    "bottleneck_hunter.watchlist.news_pipeline",
    "bottleneck_hunter.watchlist.sec_pipeline",
    "bottleneck_hunter.watchlist.options_pipeline",
    "bottleneck_hunter.watchlist.notice_pipeline",
]


class TestSemaphoreLazy:
    @pytest.mark.parametrize("module_path", PIPELINE_MODULES)
    def test_sem_is_none_at_import(self, module_path):
        """模块导入后 _SEM 应为 None（延迟初始化）。"""
        mod = importlib.import_module(module_path)
        old = mod._SEM
        mod._SEM = None
        reimported = importlib.reload(mod)
        assert reimported._SEM is None
        mod._SEM = old

    @pytest.mark.parametrize("module_path", PIPELINE_MODULES)
    def test_get_sem_creates_on_first_call(self, module_path):
        """_get_sem() 首次调用时应创建 Semaphore。"""
        mod = importlib.import_module(module_path)
        mod._SEM = None
        sem = mod._get_sem()
        assert isinstance(sem, asyncio.Semaphore)
        assert mod._SEM is sem

    @pytest.mark.parametrize("module_path", PIPELINE_MODULES)
    def test_get_sem_reuses(self, module_path):
        """_get_sem() 应复用已创建的 Semaphore。"""
        mod = importlib.import_module(module_path)
        mod._SEM = None
        first = mod._get_sem()
        second = mod._get_sem()
        assert first is second

    async def test_concurrent_limit(self):
        """验证 Semaphore 限制并发数。"""
        from bottleneck_hunter.watchlist.price_pipeline import _get_sem, _SEM
        import bottleneck_hunter.watchlist.price_pipeline as pp

        pp._SEM = None
        sem = pp._get_sem()

        active = 0
        max_active = 0

        async def task():
            nonlocal active, max_active
            async with sem:
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*[task() for _ in range(10)])
        assert max_active <= 4
