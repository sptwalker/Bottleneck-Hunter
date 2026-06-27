"""Tests for Phase 11B — 管道健康追踪 + 数据新鲜度检查。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "test.db")


def _add_ticker(store, ticker="AAPL", market="us_stock"):
    store.add({"ticker": ticker, "company_name": f"{ticker} Inc", "tier": "track", "market": market})


# ═════════════════════════════════════════════════════════
# Scheduler 精细化状态上报
# ═════════════════════════════════════════════════════════

class TestSchedulerPartialFailure:
    async def test_price_partial_reports_partial(self, store):
        """部分 ticker 失败时状态应为 partial"""
        _add_ticker(store, "AAPL")
        _add_ticker(store, "MSFT")

        from bottleneck_hunter.watchlist import scheduler
        scheduler._wl_store = store

        mock_results = {"AAPL": "ok", "MSFT": "error: timeout"}
        with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", new_callable=AsyncMock, return_value=mock_results):
            await scheduler.job_price_update("us_stock")

        statuses = store.get_pipeline_statuses()
        price_st = next(s for s in statuses if s["pipeline_name"] == "price")
        assert price_st["last_status"] == "partial"
        assert "1/2" in price_st["last_error"]

    async def test_price_all_fail_reports_error(self, store):
        """全部 ticker 失败时状态应为 error"""
        _add_ticker(store, "AAPL")

        from bottleneck_hunter.watchlist import scheduler
        scheduler._wl_store = store

        mock_results = {"AAPL": "error: connection"}
        with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", new_callable=AsyncMock, return_value=mock_results):
            await scheduler.job_price_update("us_stock")

        statuses = store.get_pipeline_statuses()
        price_st = next(s for s in statuses if s["pipeline_name"] == "price")
        assert price_st["last_status"] == "error"

    async def test_price_all_ok_reports_success(self, store):
        """全部成功时状态应为 success"""
        _add_ticker(store, "AAPL")
        _add_ticker(store, "MSFT")

        from bottleneck_hunter.watchlist import scheduler
        scheduler._wl_store = store

        mock_results = {"AAPL": "ok", "MSFT": "ok"}
        with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", new_callable=AsyncMock, return_value=mock_results):
            await scheduler.job_price_update("us_stock")

        statuses = store.get_pipeline_statuses()
        price_st = next(s for s in statuses if s["pipeline_name"] == "price")
        assert price_st["last_status"] == "success"
        assert price_st["last_error"] == ""


# ═════════════════════════════════════════════════════════
# 数据新鲜度查询
# ═════════════════════════════════════════════════════════

class TestStaleTickers:
    def test_no_snapshots_is_stale(self, store):
        """无快照的活跃 ticker 应返回为过期"""
        _add_ticker(store, "AAPL")
        stale = store.get_stale_tickers(max_age_hours=48)
        assert len(stale) == 1
        assert stale[0]["ticker"] == "AAPL"

    def test_recent_snapshot_not_stale(self, store):
        """有新快照的 ticker 不应过期"""
        _add_ticker(store, "AAPL")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store.save_snapshots([{
            "ticker": "AAPL", "date": today,
            "close": 150.0, "volume": 1000,
        }])
        stale = store.get_stale_tickers(max_age_hours=48)
        assert len(stale) == 0

    def test_old_snapshot_is_stale(self, store):
        """老快照的 ticker 应过期"""
        _add_ticker(store, "AAPL")
        old_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
        store.save_snapshots([{
            "ticker": "AAPL", "date": old_date,
            "close": 150.0, "volume": 1000,
        }])
        stale = store.get_stale_tickers(max_age_hours=48)
        assert len(stale) == 1
        assert stale[0]["ticker"] == "AAPL"


# ═════════════════════════════════════════════════════════
# Health API 端点
# ═════════════════════════════════════════════════════════

class TestHealthEndpoint:
    def test_health_returns_pipelines_and_stale(self, store):
        """/health 端点应返回管道状态和过期 ticker"""
        from fastapi.testclient import TestClient
        from bottleneck_hunter.web.watchlist_api import router, set_store

        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router, prefix="/api/watchlist")
        set_store(store)

        _add_ticker(store, "AAPL")
        store.update_pipeline_status("price", last_status="success")

        client = TestClient(app)
        resp = client.get("/api/watchlist/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "pipelines" in data
        assert "stale_tickers" in data
        assert len(data["stale_tickers"]) == 1
