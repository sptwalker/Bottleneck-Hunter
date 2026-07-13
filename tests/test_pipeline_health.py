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

    def test_recent_fetch_with_old_bar_not_stale(self, store):
        """刚抓取过(fetched_at=now)但 K线交易日很旧(如回补的老 bar)→ 不应误报未更新。

        这是用户报的 bug：一键刷新过，但因判据误用 ms.date（交易日）而非 fetched_at 恒报 stale。
        """
        _add_ticker(store, "AAPL")
        old_bar = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
        store.save_snapshots([{
            "ticker": "AAPL", "date": old_bar,   # 交易日 6 个月前
            "close": 150.0, "volume": 1000,
            "fetched_at": datetime.now(timezone.utc).isoformat(),  # 但刚抓取
        }])
        stale = store.get_stale_tickers(max_age_hours=48)
        assert len(stale) == 0, "刚刷新过就不该报未更新"

    def test_old_snapshot_is_stale(self, store):
        """超过阈值未抓取的 ticker 应过期（按 fetched_at 判定）"""
        _add_ticker(store, "AAPL")
        old_fetch = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        store.save_snapshots([{
            "ticker": "AAPL", "date": "2026-01-01",
            "close": 150.0, "volume": 1000,
            "fetched_at": old_fetch,   # 5 天前抓取
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
        from bottleneck_hunter.auth.dependencies import get_current_user

        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(router, prefix="/api/watchlist")
        app.dependency_overrides[get_current_user] = lambda: {"sub": ""}  # 测试注入用户，绕过 AuthMiddleware
        set_store(store)

        _add_ticker(store, "AAPL")
        # 端点用 include_never_fetched=False（刚加入未抓取的不算陈旧），故给 AAPL 一条 >48h 的旧快照
        store.save_snapshots([{
            "ticker": "AAPL", "date": "2020-01-01", "close": 180.0,
            "fetched_at": "2020-01-01T00:00:00+00:00",
        }])
        store.update_pipeline_status("price", last_status="success")

        client = TestClient(app)
        resp = client.get("/api/watchlist/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "pipelines" in data
        assert "stale_tickers" in data
        assert len(data["stale_tickers"]) == 1
