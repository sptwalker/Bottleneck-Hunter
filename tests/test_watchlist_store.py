"""Tests for WatchlistStore — 核心 CRUD、行情快照、新闻摘要、管道状态"""

import pytest
from bottleneck_hunter.watchlist.store import WatchlistStore


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    """创建临时 SQLite store"""
    return WatchlistStore(tmp_path / "test.db")


def _make_entry(ticker="AAPL", tier="track", **overrides):
    """构造最小 watchlist 条目 dict"""
    base = {"ticker": ticker, "company_name": f"{ticker} Inc", "tier": tier}
    base.update(overrides)
    return base


# ═════════════════════════════════════════════════════════
# 1. Watchlist CRUD
# ═════════════════════════════════════════════════════════

class TestWatchlistCRUD:
    def test_add_returns_id(self, store):
        """add() 应返回非空 entry_id"""
        entry_id = store.add(_make_entry("AAPL", "focus"))
        assert isinstance(entry_id, str) and len(entry_id) > 0

    def test_add_duplicate_ticker_raises(self, store):
        """同一 ticker 重复添加应抛 ValueError"""
        store.add(_make_entry("AAPL"))
        with pytest.raises(ValueError, match="already in watchlist"):
            store.add(_make_entry("AAPL"))

    def test_add_tier_full_raises(self, store):
        """tier 容量已满时再添加应抛 ValueError（focus 上限 6）"""
        for i in range(6):
            store.add(_make_entry(f"F{i}", "focus"))
        with pytest.raises(ValueError, match="full"):
            store.add(_make_entry("OVERFLOW", "focus"))

    def test_injected_tier_caps_scale_limits(self, store):
        """for_user 注入的 tier_caps 应决定分档容量（去硬编码验证）。

        上限 8 → 25%/25% → focus 2 / normal 2 / track 4。
        """
        from bottleneck_hunter.watchlist.tier_limits import derive_tier_caps
        us = store.for_user("u_small", tier_caps=derive_tier_caps(8, 0.25, 0.25))
        assert us._effective_tier_caps() == {"focus": 2, "normal": 2, "track": 4}
        # focus 满 2 即拒（而非默认的 6）
        us.add(_make_entry("A1", "focus"))
        us.add(_make_entry("A2", "focus"))
        with pytest.raises(ValueError, match="full"):
            us.add(_make_entry("A3", "focus"))
        # 总容量 8：填满后再加任何档都拒
        for i in range(2):
            us.add(_make_entry(f"N{i}", "normal"))
        for i in range(4):
            us.add(_make_entry(f"T{i}", "track"))
        with pytest.raises(ValueError, match="full"):
            us.add(_make_entry("OVER", "track"))

    def test_list_all_no_filter(self, store):
        """list_all() 不传 tier 返回全部条目，按 tier + composite_score 排序"""
        store.add(_make_entry("AAPL", "focus", composite_score=80))
        store.add(_make_entry("GOOG", "track", composite_score=90))
        store.add(_make_entry("MSFT", "focus", composite_score=95))

        result = store.list_all()
        assert len(result) == 3
        # focus 排在 track 前面；focus 内部按 composite_score DESC
        tickers = [r["ticker"] for r in result]
        assert tickers.index("MSFT") < tickers.index("AAPL")  # 同 tier, 95 > 80
        assert tickers.index("AAPL") < tickers.index("GOOG")  # focus < track

    def test_list_all_with_tier_filter(self, store):
        """list_all(tier='focus') 只返回 focus 条目"""
        store.add(_make_entry("AAPL", "focus"))
        store.add(_make_entry("GOOG", "track"))

        result = store.list_all(tier="focus")
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_update_fields(self, store):
        """update() 可更新 tier/notes 等字段，返回 True"""
        eid = store.add(_make_entry("AAPL", "track"))
        ok = store.update(eid, tier="focus", notes="重点关注")
        assert ok is True

        entry = store.get(eid)
        assert entry["tier"] == "focus"
        assert entry["notes"] == "重点关注"

    def test_remove(self, store):
        """remove() 硬删除条目，返回 True；再次删除返回 False"""
        eid = store.add(_make_entry("AAPL"))
        assert store.remove(eid) is True
        assert store.remove(eid) is False
        assert store.list_all() == []

    def test_get_tickers_by_market(self, store):
        """get_tickers_by_market() 按 market 分组返回 {market: [tickers]}"""
        store.add(_make_entry("AAPL", market="us_stock"))
        store.add(_make_entry("600519", market="a_stock"))
        store.add(_make_entry("GOOG", market="us_stock"))

        result = store.get_tickers_by_market()
        assert set(result["us_stock"]) == {"AAPL", "GOOG"}
        assert result["a_stock"] == ["600519"]


# ═════════════════════════════════════════════════════════
# 2. Market Snapshots
# ═════════════════════════════════════════════════════════

class TestMarketSnapshots:
    def _snap(self, ticker="AAPL", date="2025-06-01", close=190.0, **kw):
        """构造最小快照 dict"""
        base = {"ticker": ticker, "date": date, "close": close, "open": 188.0,
                "high": 192.0, "low": 187.0, "volume": 1000000}
        base.update(kw)
        return base

    def test_save_and_get_snapshots(self, store):
        """save_snapshots 返回写入数量；get_snapshots 按日期倒序"""
        snaps = [self._snap(date=f"2025-06-0{i}") for i in range(1, 4)]
        count = store.save_snapshots(snaps)
        assert count == 3

        result = store.get_snapshots("AAPL", days=90)
        assert len(result) == 3
        # 日期倒序
        assert result[0]["date"] >= result[-1]["date"]

    def test_get_latest_snapshot(self, store):
        """get_latest_snapshot 返回最新一条"""
        store.save_snapshots([self._snap(date="2025-06-01"), self._snap(date="2025-06-05")])
        latest = store.get_latest_snapshot("AAPL")
        assert latest["date"] == "2025-06-05"

    def test_get_latest_snapshot_none(self, store):
        """无数据时 get_latest_snapshot 返回 None"""
        assert store.get_latest_snapshot("NODATA") is None

    def test_same_day_replace(self, store):
        """同 ticker + 同 date 的快照应被覆盖（INSERT OR REPLACE）"""
        store.save_snapshots([self._snap(date="2025-06-01", close=190.0)])
        store.save_snapshots([self._snap(date="2025-06-01", close=200.0)])

        result = store.get_snapshots("AAPL")
        assert len(result) == 1
        assert result[0]["close"] == 200.0


# ═════════════════════════════════════════════════════════
# 3. News Digest
# ═════════════════════════════════════════════════════════

class TestNewsDigest:
    def _news(self, nid="n1", ticker="AAPL", date="2025-06-01", title="测试新闻"):
        return {"id": nid, "ticker": ticker, "date": date, "title": title}

    def test_save_and_get_news(self, store):
        """save_news 返回条数；get_news 按日期倒序"""
        items = [
            self._news("n1", date="2025-06-01", title="早报"),
            self._news("n2", date="2025-06-03", title="晚报"),
        ]
        count = store.save_news(items)
        assert count == 2

        result = store.get_news("AAPL", limit=20)
        assert len(result) == 2
        assert result[0]["date"] >= result[-1]["date"]

    def test_save_news_ignore_duplicate(self, store):
        """INSERT OR IGNORE — 重复 id 不报错，不覆盖"""
        store.save_news([self._news("n1", title="原始标题")])
        store.save_news([self._news("n1", title="新标题")])

        result = store.get_news("AAPL")
        assert len(result) == 1
        assert result[0]["title"] == "原始标题"


# ═════════════════════════════════════════════════════════
# 4. Pipeline Status
# ═════════════════════════════════════════════════════════

class TestPipelineStatus:
    def test_update_creates_then_updates(self, store):
        """首次调用自动 INSERT；后续调用 UPDATE 字段"""
        store.update_pipeline_status("price_update", last_status="running", stocks_total=10)
        statuses = store.get_pipeline_statuses()
        assert len(statuses) >= 1

        found = [s for s in statuses if s["pipeline_name"] == "price_update"]
        assert len(found) == 1
        assert found[0]["last_status"] == "running"
        assert found[0]["stocks_total"] == 10

        # 第二次更新同一管道
        store.update_pipeline_status("price_update", last_status="done", stocks_processed=10)
        statuses = store.get_pipeline_statuses()
        found = [s for s in statuses if s["pipeline_name"] == "price_update"]
        assert len(found) == 1
        assert found[0]["last_status"] == "done"
        assert found[0]["stocks_processed"] == 10

    def test_multiple_pipelines(self, store):
        """多个管道互不干扰"""
        store.update_pipeline_status("price", last_status="ok")
        store.update_pipeline_status("news", last_status="fail")

        statuses = {s["pipeline_name"]: s["last_status"] for s in store.get_pipeline_statuses()}
        assert statuses["price"] == "ok"
        assert statuses["news"] == "fail"


# ═════════════════════════════════════════════════════════
# Catalysts — upcoming window
# ═════════════════════════════════════════════════════════

class TestUpcomingCatalysts:
    def test_get_upcoming_honors_days_window(self, store):
        """get_upcoming_catalysts(days=N) 只返回未来 N 天内的催化剂（回归：days 曾被 SQL 忽略）"""
        from datetime import datetime, timedelta, timezone

        entry_id = store.add(_make_entry("NVDA"))
        today = datetime.now(timezone.utc)
        near = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        far = (today + timedelta(days=60)).strftime("%Y-%m-%d")
        store.create_catalyst(entry_id, "NVDA", "near event", expected_date=near)
        store.create_catalyst(entry_id, "NVDA", "far event", expected_date=far)

        titles_7 = {c["title"] for c in store.get_upcoming_catalysts(days=7)}
        assert "near event" in titles_7
        assert "far event" not in titles_7

        titles_90 = {c["title"] for c in store.get_upcoming_catalysts(days=90)}
        assert {"near event", "far event"} <= titles_90
