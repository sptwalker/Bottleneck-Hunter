"""Tests for watchlist_api.py — API 端点契约测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.watchlist_api import router, set_store


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "test.db")


@pytest.fixture
def client(store, monkeypatch):
    async def _ok(ticker, market):   # 上市校验走真网络，契约测试一律放行；校验本身见 TestVerifyListing
        return None
    monkeypatch.setattr("bottleneck_hunter.web.watchlist_api._verify_listing", _ok)
    app = FastAPI()
    app.include_router(router, prefix="/api/watchlist")
    set_store(store)
    from bottleneck_hunter.auth.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {"sub": "", "username": "test", "role": "admin"}
    return TestClient(app)


def _add_stock(client, ticker="AAPL", company="Apple Inc", market="us_stock", tier="track"):
    return client.post("/api/watchlist", json={
        "ticker": ticker,
        "company_name": company,
        "market": market,
        "tier": tier,
    })


class TestListWatchlist:
    def test_empty_list(self, client):
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert data["total"] == 0

    def test_list_with_entries(self, client):
        _add_stock(client, "AAPL")
        _add_stock(client, "MSFT", "Microsoft")
        resp = client.get("/api/watchlist")
        data = resp.json()
        assert data["total"] == 2
        assert len(data["entries"]) == 2

    def test_list_filter_by_tier(self, client):
        _add_stock(client, "AAPL", tier="focus")
        _add_stock(client, "MSFT", tier="track")
        resp = client.get("/api/watchlist?tier=focus")
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["ticker"] == "AAPL"

    def test_counts_correct(self, client):
        _add_stock(client, "AAPL", tier="focus")
        _add_stock(client, "MSFT", tier="normal")
        _add_stock(client, "GOOG", "Google", tier="track")
        resp = client.get("/api/watchlist")
        counts = resp.json()["counts"]
        assert counts["focus"] == 1
        assert counts["normal"] == 1
        assert counts["track"] == 1


class TestAddToWatchlist:
    def test_add_success(self, client):
        resp = _add_stock(client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "added"
        assert "id" in data

    def test_add_duplicate_409(self, client):
        _add_stock(client, "AAPL")
        resp = _add_stock(client, "AAPL")
        assert resp.status_code == 409

    def test_add_tier_full_409(self, client):
        for i in range(6):
            _add_stock(client, f"STOCK{i}", f"Company {i}", tier="focus")
        resp = _add_stock(client, "OVERFLOW", "Overflow Inc", tier="focus")
        assert resp.status_code == 409


class TestGetEntry:
    def test_get_existing(self, client):
        add_resp = _add_stock(client)
        entry_id = add_resp.json()["id"]
        resp = client.get(f"/api/watchlist/{entry_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "AAPL"
        assert "latest_snapshot" in data
        assert "recent_news" in data

    def test_get_not_found(self, client):
        resp = client.get("/api/watchlist/nonexistent")
        assert resp.status_code == 404


class TestDeleteEntry:
    def test_delete_success(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.delete(f"/api/watchlist/{entry_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"

        verify = client.get(f"/api/watchlist/{entry_id}")
        assert verify.status_code == 404

    def test_delete_not_found(self, client):
        resp = client.delete("/api/watchlist/nonexistent")
        assert resp.status_code == 404


class TestUpdateEntry:
    def test_update_tier(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.patch(f"/api/watchlist/{entry_id}", json={"tier": "focus"})
        assert resp.status_code == 200

        verify = client.get(f"/api/watchlist/{entry_id}")
        assert verify.json()["tier"] == "focus"

    def test_update_notes(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.patch(f"/api/watchlist/{entry_id}", json={"notes": "test note"})
        assert resp.status_code == 200

    def test_update_not_found(self, client):
        resp = client.patch("/api/watchlist/nonexistent", json={"tier": "focus"})
        assert resp.status_code == 404


class TestBatchDelete:
    def test_batch_delete_multiple(self, client):
        id1 = _add_stock(client, "AAPL").json()["id"]
        id2 = _add_stock(client, "MSFT", "Microsoft").json()["id"]
        _add_stock(client, "GOOG", "Google")

        resp = client.post("/api/watchlist/batch-delete", json={"ids": [id1, id2]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] == 2

        remaining = client.get("/api/watchlist").json()
        assert remaining["total"] == 1

    def test_batch_delete_empty_ids_400(self, client):
        resp = client.post("/api/watchlist/batch-delete", json={"ids": []})
        assert resp.status_code == 400

    def test_batch_delete_nonexistent(self, client):
        resp = client.post("/api/watchlist/batch-delete", json={"ids": ["fake1", "fake2"]})
        assert resp.status_code == 200
        assert resp.json()["removed"] == 0


class TestBatchTier:
    def test_batch_tier_update(self, client):
        id1 = _add_stock(client, "AAPL").json()["id"]
        id2 = _add_stock(client, "MSFT", "Microsoft").json()["id"]

        resp = client.put("/api/watchlist/batch-tier", json={"ids": [id1, id2], "tier": "focus"})
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        e1 = client.get(f"/api/watchlist/{id1}").json()
        assert e1["tier"] == "focus"

    def test_batch_tier_invalid_tier_400(self, client):
        resp = client.put("/api/watchlist/batch-tier", json={"ids": ["a"], "tier": "invalid"})
        assert resp.status_code == 400

    def test_batch_tier_empty_ids_400(self, client):
        resp = client.put("/api/watchlist/batch-tier", json={"ids": [], "tier": "focus"})
        assert resp.status_code == 400


class TestSubResources:
    def test_snapshots_empty(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.get(f"/api/watchlist/{entry_id}/snapshots")
        assert resp.status_code == 200
        assert resp.json()["snapshots"] == []

    def test_news_empty(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.get(f"/api/watchlist/{entry_id}/news")
        assert resp.status_code == 200
        assert resp.json()["news"] == []

    def test_filings_empty(self, client):
        entry_id = _add_stock(client).json()["id"]
        resp = client.get(f"/api/watchlist/{entry_id}/filings")
        assert resp.status_code == 200
        assert resp.json()["filings"] == []

    def test_sub_resource_404(self, client):
        resp = client.get("/api/watchlist/nonexistent/snapshots")
        assert resp.status_code == 404


class TestOverviewOnDemandFetch:
    """反向分析/手动加入的新条目无快照 → 打开详情概览须按需拉取（否则永远「暂无行情数据」）。"""

    def test_overview_triggers_fetch_when_no_snapshot(self, client, store, monkeypatch):
        entry_id = _add_stock(client, "NEWCO").json()["id"]
        called = {}

        async def fake_fetch_one(ticker, st, days=180, market="us_stock", cache=None):
            called["ticker"] = ticker
            called["market"] = market
            st.save_snapshots([{"ticker": ticker, "date": "2026-08-06", "close": 12.5, "market": market}])
            return "ok"

        monkeypatch.setattr("bottleneck_hunter.watchlist.price_pipeline._fetch_one", fake_fetch_one)
        resp = client.get(f"/api/watchlist/{entry_id}/overview")
        assert resp.status_code == 200
        assert called.get("ticker") == "NEWCO"          # 空快照 → 触发了按需抓取
        assert resp.json()["latest_snapshot"]["close"] == 12.5   # 抓取结果已回填

    def test_overview_skips_fetch_for_isin(self):
        # 场外基金 ISIN 无公开源 → 端点须跳过按需抓取（否则 yfinance 按 ISIN 检索必然 429/超时）
        from bottleneck_hunter.web.watchlist_api import _ISIN_RE
        assert _ISIN_RE.match("IE00B4L5Y983")   # 命中即 get_overview 短路，不调 _fetch_one
        assert not _ISIN_RE.match("NVDA")


class TestVerifyListing:
    """入池上市守卫：以腾讯行情后缀判主板，拒 OTC 粉单/查无/B股/非美A市场。"""

    @staticmethod
    def _run(monkeypatch, ticker, market, body, raise_exc=False):
        import asyncio

        from bottleneck_hunter.web import watchlist_api as wa
        seen = {}

        class _R:
            content = body.encode("gbk")

        class _C:
            async def get(self, url, timeout=None):
                seen["url"] = url
                if raise_exc:
                    raise OSError("down")
                return _R()
        monkeypatch.setattr("bottleneck_hunter.watchlist.retry.get_http_client", lambda: _C())
        asyncio.run(wa._verify_listing(ticker, market))
        return seen["url"]

    def test_main_boards_pass(self, monkeypatch):
        assert self._run(monkeypatch, "NVDA", "us_stock", 'v_usNVDA="200~英伟达~NVDA.OQ~227.21";').endswith("usNVDA")
        self._run(monkeypatch, "TSM", "us_stock", 'v_usTSM="200~台积电~TSM.N~456";')
        self._run(monkeypatch, "SPY", "us_stock", 'v_usSPY="200~标普ETF~SPY.AM~764";')
        assert self._run(monkeypatch, "BRK-B", "us_stock", 'v_usBRK.B="200~伯克希尔B~BRK.B.N~502";').endswith("usBRK.B")
        url = self._run(monkeypatch, "600519.SS", "a_stock", 'v_sh600519="1~贵州茅台~600519~1241";')
        assert url.endswith("sh600519")

    @pytest.mark.parametrize("ticker,market,body,msg", [
        ("RNECY", "us_stock", 'v_usRNECY="200~Renesas~RNECY.PS~11.13";', "场外粉单"),
        ("HAM", "us_stock", 'v_pv_none_match="1";', "查无此代码"),
        ("900901.SS", "a_stock", "", "不是沪深北 A股"),
        ("0700.HK", "hk_stock", "", "只支持美股与 A股"),
        ("600519.SS", "us_stock", "", "不像美股代码"),
        ("NVDA", "a_stock", "", "不是 A股代码"),
        ("AAPL,SH600519", "us_stock", "", "不像美股代码"),
    ])
    def test_rejects(self, monkeypatch, ticker, market, body, msg):
        with pytest.raises(ValueError, match=msg):
            self._run(monkeypatch, ticker, market, body)

    def test_fail_closed_when_source_down(self, monkeypatch):
        with pytest.raises(ValueError, match="暂不可用"):
            self._run(monkeypatch, "NVDA", "us_stock", "", raise_exc=True)

    def test_api_returns_409_with_reason(self, store, monkeypatch):
        from bottleneck_hunter.auth.dependencies import get_current_user
        app = FastAPI()
        app.include_router(router, prefix="/api/watchlist")
        set_store(store)
        app.dependency_overrides[get_current_user] = lambda: {"sub": "", "username": "t", "role": "admin"}

        async def _reject(ticker, market):
            raise ValueError(f"{ticker} 场外粉单")
        monkeypatch.setattr("bottleneck_hunter.web.watchlist_api._verify_listing", _reject)
        resp = _add_stock(TestClient(app), "rnecy")
        assert resp.status_code == 409 and "RNECY 场外粉单" in resp.json()["detail"]
        assert store.get_by_ticker("RNECY") is None
