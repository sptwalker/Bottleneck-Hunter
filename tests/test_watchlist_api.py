"""Tests for watchlist_api.py — API 端点契约测试。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bottleneck_hunter.web.watchlist_api import router, set_store
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "test.db")


@pytest.fixture
def client(store):
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
