"""Tests for 8B.4 — 闭环反馈系统：催化剂时效 + 复盘 CRUD + 经验卡片 + API 端点"""

import json
import pytest
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "test.db")
    s = WatchlistStore(db)

    entry_id = s.add({
        "ticker": "AAPL",
        "company_name": "Apple Inc",
        "market": "us_stock",
        "sector": "科技",
        "tier": "track",
    })

    macro_id = s.create_macro_strategy({"market_summary": "市场偏多"})
    strat_id = s.create_strategic_plan(macro_id, {
        "target_allocation": [{"ticker": "AAPL", "weight": 0.15, "action": "buy"}],
    })

    return s, entry_id, macro_id, strat_id


# ─────────────────────────────────────────────────────────
# 催化剂时效管理
# ─────────────────────────────────────────────────────────

class TestCatalystExpiry:
    def test_expire_past_catalysts(self, store):
        s, entry_id, *_ = store
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "Q3 财报", "earnings",
                          expected_date=yesterday, impact_level="high")
        s.create_catalyst(entry_id, "AAPL", "产品发布", "product",
                          expected_date=yesterday, impact_level="medium")

        count = s.expire_past_catalysts()
        assert count == 2

        catalysts = s.get_catalysts_for_entry(entry_id, active_only=True)
        assert len(catalysts) == 0

    def test_no_expire_future_catalysts(self, store):
        s, entry_id, *_ = store
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "未来事件", "event",
                          expected_date=tomorrow)

        count = s.expire_past_catalysts()
        assert count == 0

        catalysts = s.get_catalysts_for_entry(entry_id, active_only=True)
        assert len(catalysts) == 1

    def test_no_expire_already_expired(self, store):
        s, entry_id, *_ = store
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        cid = s.create_catalyst(entry_id, "AAPL", "已过期", "event",
                                expected_date=yesterday)
        s.update_catalyst_status(cid, "expired")

        count = s.expire_past_catalysts()
        assert count == 0

    def test_get_expiring_catalysts(self, store):
        s, entry_id, *_ = store
        in_3_days = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
        in_10_days = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

        s.create_catalyst(entry_id, "AAPL", "即将到期", "earnings",
                          expected_date=in_3_days)
        s.create_catalyst(entry_id, "AAPL", "还早", "product",
                          expected_date=in_10_days)

        expiring = s.get_expiring_catalysts(days=7)
        assert len(expiring) == 1
        assert expiring[0]["title"] == "即将到期"

    def test_expire_idempotent(self, store):
        s, entry_id, *_ = store
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "测试", "event",
                          expected_date=yesterday)

        count1 = s.expire_past_catalysts()
        count2 = s.expire_past_catalysts()
        assert count1 == 1
        assert count2 == 0


# ─────────────────────────────────────────────────────────
# Auto Reviews CRUD
# ─────────────────────────────────────────────────────────

class TestAutoReviews:
    def test_create_and_get_review(self, store):
        s, *_ = store
        rid = s.create_auto_review(
            sim_trade_id="trade_001",
            ticker="AAPL",
            review_type="trade_close",
            entry_price=180.0,
            exit_price=195.0,
            return_pct=8.33,
            result_json={"what_went_right": ["入场时机好"], "trade_quality_score": 8},
            lessons_learned="入场时机选择正确",
            experience_card={"title": "测试经验", "content": "买在支撑位"},
        )
        assert rid

        review = s.get_auto_review(rid)
        assert review is not None
        assert review["ticker"] == "AAPL"
        assert review["entry_price"] == 180.0
        assert review["exit_price"] == 195.0
        assert review["return_pct"] == 8.33
        assert review["result_json"]["trade_quality_score"] == 8
        assert review["experience_card"]["title"] == "测试经验"

    def test_get_auto_reviews_list(self, store):
        s, *_ = store
        s.create_auto_review("t1", "AAPL", return_pct=5.0)
        s.create_auto_review("t2", "MSFT", return_pct=-3.0)
        s.create_auto_review("t3", "AAPL", return_pct=10.0)

        all_reviews = s.get_auto_reviews()
        assert len(all_reviews) == 3

        aapl_reviews = s.get_auto_reviews(ticker="AAPL")
        assert len(aapl_reviews) == 2

    def test_get_auto_reviews_limit(self, store):
        s, *_ = store
        for i in range(5):
            s.create_auto_review(f"t{i}", "AAPL")

        limited = s.get_auto_reviews(limit=3)
        assert len(limited) == 3

    def test_get_nonexistent_review(self, store):
        s, *_ = store
        assert s.get_auto_review("nonexistent") is None

    def test_get_trades_without_review(self, store):
        s, *_ = store
        account = s.get_sim_account()

        tid_buy = s.create_sim_trade(
            account["id"], "AAPL", "buy", 100, 180.0, 18000.0)
        tid_sell = s.create_sim_trade(
            account["id"], "AAPL", "sell", 100, 195.0, 19500.0)

        unreviewed = s.get_trades_without_review()
        assert len(unreviewed) == 1
        assert unreviewed[0]["id"] == tid_sell

        s.create_auto_review(tid_sell, "AAPL")
        unreviewed = s.get_trades_without_review()
        assert len(unreviewed) == 0

    def test_trades_without_review_ignores_buys(self, store):
        s, *_ = store
        account = s.get_sim_account()
        s.create_sim_trade(account["id"], "AAPL", "buy", 50, 180.0, 9000.0)

        unreviewed = s.get_trades_without_review()
        assert len(unreviewed) == 0


# ─────────────────────────────────────────────────────────
# Experience Cards CRUD
# ─────────────────────────────────────────────────────────

class TestExperienceCards:
    def test_create_and_get_card(self, store):
        s, *_ = store
        cid = s.create_experience_card(
            scope="ticker",
            scope_key="AAPL",
            category="lesson",
            title="支撑位入场",
            content="在关键支撑位附近入场，胜率较高",
            evidence=["AAPL: +8.3% (15d)"],
            confidence=0.75,
            source_review_id="rev_001",
        )
        assert cid

        cards = s.get_experience_cards()
        assert len(cards) == 1
        assert cards[0]["title"] == "支撑位入场"
        assert cards[0]["scope"] == "ticker"
        assert cards[0]["scope_key"] == "AAPL"
        assert cards[0]["confidence"] == 0.75
        assert cards[0]["evidence"] == ["AAPL: +8.3% (15d)"]

    def test_get_cards_by_scope(self, store):
        s, *_ = store
        s.create_experience_card("global", "", "lesson", "全局经验", "内容")
        s.create_experience_card("ticker", "AAPL", "pattern", "AAPL模式", "内容")
        s.create_experience_card("sector", "科技", "rule", "科技规则", "内容")

        global_cards = s.get_experience_cards(scope="global")
        assert len(global_cards) == 1
        assert global_cards[0]["title"] == "全局经验"

        ticker_cards = s.get_experience_cards(scope="ticker", scope_key="AAPL")
        assert len(ticker_cards) == 1

    def test_get_relevant_cards(self, store):
        s, *_ = store
        s.create_experience_card("global", "", "lesson", "通用经验", "内容", confidence=0.8)
        s.create_experience_card("ticker", "AAPL", "pattern", "AAPL经验", "内容", confidence=0.9)
        s.create_experience_card("sector", "科技", "rule", "科技经验", "内容", confidence=0.7)
        s.create_experience_card("ticker", "MSFT", "lesson", "MSFT经验", "内容", confidence=0.6)

        relevant = s.get_relevant_cards("AAPL", "科技", limit=5)
        assert len(relevant) == 3
        assert relevant[0]["title"] == "AAPL经验"  # highest confidence
        assert relevant[1]["title"] == "通用经验"
        assert relevant[2]["title"] == "科技经验"

    def test_increment_applied(self, store):
        s, *_ = store
        cid = s.create_experience_card("global", "", "lesson", "测试", "内容")

        cards = s.get_experience_cards()
        assert cards[0]["applied_count"] == 0

        s.increment_card_applied(cid)
        s.increment_card_applied(cid)
        s.increment_card_applied(cid)

        cards = s.get_experience_cards()
        assert cards[0]["applied_count"] == 3

    def test_delete_card(self, store):
        s, *_ = store
        cid = s.create_experience_card("global", "", "lesson", "待删除", "内容")

        ok = s.delete_experience_card(cid)
        assert ok is True

        cards = s.get_experience_cards()
        assert len(cards) == 0

    def test_delete_nonexistent_card(self, store):
        s, *_ = store
        ok = s.delete_experience_card("nonexistent")
        assert ok is False


# ─────────────────────────────────────────────────────────
# Trade Feedback History
# ─────────────────────────────────────────────────────────

class TestFeedbackHistory:
    def test_get_feedback_history(self, store):
        s, *_ = store
        s.create_trade_feedback("plan_1", "AAPL", "rejection", "风险过高")
        s.create_trade_feedback("plan_2", "MSFT", "rejection", "估值过高")

        history = s.get_trade_feedback_history(limit=50)
        assert len(history) == 2
        assert history[0]["ticker"] in ("AAPL", "MSFT")

    def test_empty_feedback_history(self, store):
        s, *_ = store
        history = s.get_trade_feedback_history()
        assert history == []


# ─────────────────────────────────────────────────────────
# Catalyst Monitor — check_catalyst_expiry
# ─────────────────────────────────────────────────────────

class TestCatalystMonitor:
    @pytest.mark.asyncio
    async def test_check_catalyst_expiry_with_expired(self, store):
        s, entry_id, *_ = store
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "过期催化剂", "event",
                          expected_date=yesterday)

        from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry

        events = []
        async for evt in check_catalyst_expiry(s):
            events.append(evt)

        assert any(e.get("event") == "catalyst_expired" for e in events)

    @pytest.mark.asyncio
    async def test_check_catalyst_expiry_with_upcoming(self, store):
        s, entry_id, *_ = store
        in_3_days = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "即将到期", "event",
                          expected_date=in_3_days)

        from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry

        events = []
        async for evt in check_catalyst_expiry(s):
            events.append(evt)

        assert any(e.get("event") == "catalyst_expiring_soon" for e in events)

    @pytest.mark.asyncio
    async def test_check_catalyst_expiry_no_events(self, store):
        s, *_ = store

        from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry

        events = []
        async for evt in check_catalyst_expiry(s):
            events.append(evt)

        assert len(events) == 0


# ─────────────────────────────────────────────────────────
# Decision API — 复盘 & 经验卡片端点
# ─────────────────────────────────────────────────────────

class TestDecisionAPIFeedback:
    @pytest.fixture
    def client(self, store):
        s, *_ = store
        from fastapi.testclient import TestClient
        from bottleneck_hunter.web.decision_api import router, set_store
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router, prefix="/api/decision")
        set_store(s)
        from bottleneck_hunter.auth.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: {"sub": "", "username": "test", "role": "admin"}
        return TestClient(app), s

    def test_reviews_empty(self, client, store):
        c, s = client
        resp = c.get("/api/decision/reviews")
        assert resp.status_code == 200
        assert resp.json()["reviews"] == []

    def test_reviews_with_data(self, client, store):
        c, s = client
        s.create_auto_review("t1", "AAPL", return_pct=5.0,
                             result_json={"trade_quality_score": 7})

        resp = c.get("/api/decision/reviews")
        assert resp.status_code == 200
        reviews = resp.json()["reviews"]
        assert len(reviews) == 1
        assert reviews[0]["ticker"] == "AAPL"

    def test_review_detail(self, client, store):
        c, s = client
        rid = s.create_auto_review("t1", "AAPL", return_pct=5.0,
                                   result_json={"trade_quality_score": 7})

        resp = c.get(f"/api/decision/reviews/{rid}")
        assert resp.status_code == 200
        assert resp.json()["review"]["id"] == rid

    def test_review_detail_not_found(self, client, store):
        c, _ = client
        resp = c.get("/api/decision/reviews/nonexistent")
        assert resp.status_code == 404

    def test_experience_empty(self, client, store):
        c, _ = client
        resp = c.get("/api/decision/experience")
        assert resp.status_code == 200
        assert resp.json()["cards"] == []

    def test_experience_with_data(self, client, store):
        c, s = client
        s.create_experience_card("global", "", "lesson", "测试卡片", "内容")

        resp = c.get("/api/decision/experience")
        assert resp.status_code == 200
        cards = resp.json()["cards"]
        assert len(cards) == 1
        assert cards[0]["title"] == "测试卡片"

    def test_experience_delete(self, client, store):
        c, s = client
        cid = s.create_experience_card("global", "", "lesson", "待删除", "内容")

        resp = c.delete(f"/api/decision/experience/{cid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        resp2 = c.get("/api/decision/experience")
        assert len(resp2.json()["cards"]) == 0

    def test_experience_delete_not_found(self, client, store):
        c, _ = client
        resp = c.delete("/api/decision/experience/nonexistent")
        assert resp.status_code == 404

    def test_feedback_empty(self, client, store):
        c, _ = client
        resp = c.get("/api/decision/feedback")
        assert resp.status_code == 200
        assert resp.json()["feedback"] == []

    def test_feedback_with_data(self, client, store):
        c, s = client
        s.create_trade_feedback("plan_1", "AAPL", "rejection", "太贵了")

        resp = c.get("/api/decision/feedback")
        assert resp.status_code == 200
        feedback = resp.json()["feedback"]
        assert len(feedback) == 1
        assert feedback[0]["ticker"] == "AAPL"
        assert feedback[0]["reason"] == "太贵了"

    def test_reviews_filter_by_ticker(self, client, store):
        c, s = client
        s.create_auto_review("t1", "AAPL", return_pct=5.0)
        s.create_auto_review("t2", "MSFT", return_pct=-2.0)

        resp = c.get("/api/decision/reviews?ticker=AAPL")
        assert resp.status_code == 200
        reviews = resp.json()["reviews"]
        assert len(reviews) == 1
        assert reviews[0]["ticker"] == "AAPL"

    def test_experience_filter_by_scope(self, client, store):
        c, s = client
        s.create_experience_card("global", "", "lesson", "全局", "内容")
        s.create_experience_card("ticker", "AAPL", "pattern", "个股", "内容")

        resp = c.get("/api/decision/experience?scope=global")
        assert resp.status_code == 200
        assert len(resp.json()["cards"]) == 1
