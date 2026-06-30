"""Tests for 8B.2 — L3 战术计划、L4 执行方案、投委会评审"""

import asyncio
import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from bottleneck_hunter.watchlist.store import WatchlistStore


# ─────────────────────────────────────────────────────────
# 辅助工具
# ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    """创建临时 SQLite store 并写入最小测试数据"""
    db = str(tmp_path / "test.db")
    s = WatchlistStore(db)

    entry_id = s.add({
        "ticker": "AAPL",
        "company_name": "Apple Inc",
        "market": "us_stock",
        "sector": "科技",
        "tier": "track",
    })

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s.save_snapshots([{
        "ticker": "AAPL",
        "date": today,
        "close": 190.0,
        "change_pct": 1.5,
        "volume": 5000000,
        "rsi_14": 55.0,
        "sma_50": 185.0,
    }])

    macro_id = s.create_macro_strategy({
        "market_summary": "市场整体偏多，科技板块强势",
        "risk_level": "medium",
        "sector_outlook": {"科技": "bullish"},
    })

    strat_id = s.create_strategic_plan(macro_id, {
        "target_allocation": [
            {"ticker": "AAPL", "weight": 0.15, "action": "buy"}
        ],
        "cash_reserve": 0.3,
    })

    return s, entry_id, macro_id, strat_id


def _mock_llm_response(content: dict):
    """构建 mock LLM 返回"""
    llm = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(content, ensure_ascii=False)
    llm.invoke = MagicMock(return_value=msg)
    return llm


TACTICAL_RESPONSE = {
    "tactical_plans": [
        {
            "ticker": "AAPL",
            "action": "buy",
            "entry_plan": {"price_range": [185, 190], "method": "limit"},
            "exit_plan": {"stop_loss": 175, "take_profit": 210},
            "catalyst_watch": ["Q3财报", "新品发布"],
            "confidence": 8,
            "reasoning": "估值合理，催化剂密集",
        }
    ],
    "priority_ranking": ["AAPL"],
}

EXECUTION_RESPONSE = {
    "execution_plans": [
        {
            "ticker": "AAPL",
            "action": "buy",
            "shares": 50,
            "target_price": 188.0,
            "amount": 9400,
            "method": "split",
            "priority": 8,
            "confidence": 7,
            "reasoning": "分两批建仓",
        }
    ],
    "execution_summary": {"total_amount": 9400, "cash_after": 90600},
    "skipped_plans": [],
}

REVIEW_RESPONSE = {
    "vote": "approve",
    "confidence": 7,
    "risk_score": 5,
    "key_concerns": ["科技集中度偏高"],
    "suggestions": [],
    "strengths": ["止损明确"],
    "overall_assessment": "总体可接受",
}

CONSENSUS_RESPONSE = {
    "final_verdict": "approved",
    "approval_rate": 75,
    "vote_detail": {
        "risk_officer": {"vote": "approve", "confidence": 7},
        "growth_investor": {"vote": "approve", "confidence": 8},
        "value_investor": {"vote": "approve_with_modification", "confidence": 6},
        "contrarian": {"vote": "reject", "confidence": 7},
    },
    "consensus_modifications": [],
    "final_execution_plan": [
        {"ticker": "AAPL", "action": "buy", "shares": 50, "confidence": 7}
    ],
    "key_risks_flagged": ["科技集中度"],
    "minority_opinions": [],
    "summary": "3 票赞成，1 票反对，批准执行",
}


# ─────────────────────────────────────────────────────────
# L3 战术计划引擎
# ─────────────────────────────────────────────────────────

class TestRunTacticalPlans:
    @pytest.mark.asyncio
    async def test_generates_tactical_plan(self, store):
        s, entry_id, macro_id, strat_id = store
        llm = _mock_llm_response(TACTICAL_RESPONSE)

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
            events = []
            async for evt in run_tactical_plans(s):
                events.append(evt)

        event_types = [e["event"] for e in events]
        assert "decision_start" in event_types
        assert "decision_done" in event_types

        done_evt = next(e for e in events if e["event"] == "decision_done")
        assert done_evt["data"]["plan_count"] == 1

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        plans = s.get_tactical_plans_by_date(today)
        assert len(plans) == 1
        assert plans[0]["ticker"] == "AAPL"
        assert plans[0]["action"] == "buy"

    @pytest.mark.asyncio
    async def test_no_strategic_plan(self, tmp_path):
        db = str(tmp_path / "empty.db")
        s = WatchlistStore(db)
        s.create_macro_strategy({"market_summary": "ok"})

        from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
        events = []
        async for evt in run_tactical_plans(s):
            events.append(evt)

        assert any(e["event"] == "decision_error" for e in events)

    @pytest.mark.asyncio
    async def test_no_macro_strategy(self, tmp_path):
        db = str(tmp_path / "empty2.db")
        s = WatchlistStore(db)

        from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
        events = []
        async for evt in run_tactical_plans(s):
            events.append(evt)

        assert any(e["event"] == "decision_error" for e in events)


# ─────────────────────────────────────────────────────────
# L4 执行方案引擎
# ─────────────────────────────────────────────────────────

class TestRunExecutionPlans:
    @pytest.mark.asyncio
    async def test_generates_execution_plan(self, store):
        s, entry_id, macro_id, strat_id = store

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.create_tactical_plan(strat_id, entry_id, "AAPL", today, {
            "action": "buy", "confidence": 8,
            "entry_plan": {"price": 188}, "exit_plan": {"stop_loss": 175},
        })

        llm = _mock_llm_response(EXECUTION_RESPONSE)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
            events = []
            async for evt in run_execution_plans(s):
                events.append(evt)

        event_types = [e["event"] for e in events]
        assert "decision_done" in event_types

        done_evt = next(e for e in events if e["event"] == "decision_done")
        assert done_evt["data"]["plan_count"] == 1

        pending = s.get_pending_executions()
        assert len(pending) == 1
        assert pending[0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_all_hold_skips_l4(self, store):
        s, entry_id, macro_id, strat_id = store

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s.create_tactical_plan(strat_id, entry_id, "AAPL", today, {
            "action": "hold", "confidence": 5,
        })

        from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
        events = []
        async for evt in run_execution_plans(s):
            events.append(evt)

        assert any("无需生成执行方案" in e["data"].get("message", "") for e in events)

    @pytest.mark.asyncio
    async def test_no_tactical_plans(self, store):
        s, *_ = store
        from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
        events = []
        async for evt in run_execution_plans(s):
            events.append(evt)

        assert any("无 L3 战术计划" in e["data"].get("message", "") for e in events)


# ─────────────────────────────────────────────────────────
# 投委会
# ─────────────────────────────────────────────────────────

class TestCommittee:
    @pytest.mark.asyncio
    async def test_full_review_flow(self, store):
        s, entry_id, macro_id, strat_id = store

        exec_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 50, "amount": 9400,
            "target_price": 188, "confidence": 7,
        })

        pending = s.get_pending_executions()
        assert len(pending) == 1

        review_llm = _mock_llm_response(REVIEW_RESPONSE)
        consensus_llm = _mock_llm_response(CONSENSUS_RESPONSE)

        call_count = {"n": 0}
        def mock_get_llm(provider_hint=None, position=None):
            call_count["n"] += 1
            if call_count["n"] <= 4:
                return review_llm, provider_hint or "deepseek", "mock-model"
            return consensus_llm, "deepseek", "mock-model"

        with patch("bottleneck_hunter.watchlist.committee.get_llm_for_position", side_effect=mock_get_llm):
            from bottleneck_hunter.watchlist.committee import run_committee_review
            events = []
            async for evt in run_committee_review(s, pending):
                events.append(evt)

        event_types = [e["event"] for e in events]
        assert "committee_start" in event_types
        assert "committee_reviews_done" in event_types
        assert "committee_plan_done" in event_types
        assert "committee_done" in event_types

        reviews = s.get_reviews_for_execution(exec_id)
        assert len(reviews) == 4

    @pytest.mark.asyncio
    async def test_fallback_consensus(self):
        from bottleneck_hunter.watchlist.committee import _fallback_consensus

        reviews = {
            "risk_officer": {"vote": "approve", "confidence": 7},
            "growth_investor": {"vote": "approve", "confidence": 8},
            "value_investor": {"vote": "approve_with_modification", "confidence": 6},
            "contrarian": {"vote": "reject", "confidence": 7},
        }
        result = _fallback_consensus(reviews)
        assert result["final_verdict"] == "approved"
        assert result["approval_rate"] == 75

    @pytest.mark.asyncio
    async def test_fallback_consensus_rejected(self):
        from bottleneck_hunter.watchlist.committee import _fallback_consensus

        reviews = {
            "risk_officer": {"vote": "reject", "confidence": 8},
            "growth_investor": {"vote": "reject", "confidence": 7},
            "value_investor": {"vote": "reject", "confidence": 6},
            "contrarian": {"vote": "approve", "confidence": 5},
        }
        result = _fallback_consensus(reviews)
        assert result["final_verdict"] == "rejected"
        assert result["approval_rate"] == 25

    @pytest.mark.asyncio
    async def test_needs_discussion_split_vote(self):
        from bottleneck_hunter.watchlist.committee import _needs_discussion

        reviews = {
            "risk_officer": {"vote": "approve", "confidence": 7},
            "growth_investor": {"vote": "approve", "confidence": 8},
            "value_investor": {"vote": "reject", "confidence": 6},
            "contrarian": {"vote": "reject", "confidence": 7},
        }
        assert _needs_discussion(reviews) is True

    @pytest.mark.asyncio
    async def test_needs_discussion_large_confidence_gap(self):
        from bottleneck_hunter.watchlist.committee import _needs_discussion

        reviews = {
            "risk_officer": {"vote": "approve", "confidence": 3},
            "growth_investor": {"vote": "approve", "confidence": 9},
            "value_investor": {"vote": "approve", "confidence": 5},
            "contrarian": {"vote": "approve", "confidence": 5},
        }
        assert _needs_discussion(reviews) is True

    @pytest.mark.asyncio
    async def test_no_discussion_needed(self):
        from bottleneck_hunter.watchlist.committee import _needs_discussion

        reviews = {
            "risk_officer": {"vote": "approve", "confidence": 7},
            "growth_investor": {"vote": "approve", "confidence": 8},
            "value_investor": {"vote": "approve", "confidence": 6},
            "contrarian": {"vote": "approve", "confidence": 7},
        }
        assert _needs_discussion(reviews) is False


# ─────────────────────────────────────────────────────────
# Store CRUD 集成
# ─────────────────────────────────────────────────────────

class TestStoreCRUD:
    def test_tactical_plan_roundtrip(self, store):
        s, entry_id, macro_id, strat_id = store
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        plan_id = s.create_tactical_plan(strat_id, entry_id, "AAPL", today, {
            "action": "buy", "confidence": 8,
            "entry_plan": {"price": 188},
            "exit_plan": {"stop_loss": 175},
            "catalyst_watch": ["Q3财报"],
        })

        plans = s.get_tactical_plans_by_date(today)
        assert len(plans) == 1
        assert plans[0]["id"] == plan_id
        assert plans[0]["action"] == "buy"

        by_ticker = s.get_tactical_plan_for_ticker("AAPL", today)
        assert by_ticker is not None
        assert by_ticker["ticker"] == "AAPL"

    def test_execution_plan_roundtrip(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 50, "amount": 9400,
            "target_price": 188, "method": "limit",
            "priority": 8, "confidence": 7,
        })

        plan = s.get_execution_plan(plan_id)
        assert plan is not None
        assert plan["ticker"] == "AAPL"
        assert plan["shares"] == 50

        pending = s.get_pending_executions()
        assert any(p["id"] == plan_id for p in pending)

        s.confirm_execution(plan_id)
        confirmed = s.get_execution_plan(plan_id)
        assert confirmed["status"] == "confirmed"

    def test_execution_reject(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 30,
        })

        s.reject_execution(plan_id, "风险过高")
        plan = s.get_execution_plan(plan_id)
        assert plan["status"] == "rejected"

    def test_committee_review_roundtrip(self, store):
        s, *_ = store
        rid = s.create_committee_review(
            execution_plan_id="exec_1",
            member_role="risk_officer",
            model_provider="deepseek",
            model_name="deepseek-chat",
            result_json=REVIEW_RESPONSE,
        )
        reviews = s.get_reviews_for_execution("exec_1")
        assert len(reviews) == 1
        assert reviews[0]["id"] == rid
        assert reviews[0]["vote"] == "approve"

    def test_committee_consensus_roundtrip(self, store):
        s, *_ = store
        cid = s.create_committee_consensus(
            execution_plan_id="exec_1",
            result_json=CONSENSUS_RESPONSE,
        )
        conn = s._connect()
        try:
            row = conn.execute("SELECT * FROM committee_consensus WHERE id = ?", (cid,)).fetchone()
            assert row is not None
            assert dict(row)["final_verdict"] == "approved"
        finally:
            conn.close()

    def test_sim_account_default(self, store):
        s, *_ = store
        account = s.get_sim_account()
        assert account is not None
        assert account.get("cash_balance", 0) > 0


# ─────────────────────────────────────────────────────────
# API 端点 smoke test（不启 LLM，仅验证路由可达）
# ─────────────────────────────────────────────────────────

class TestDecisionAPI:
    @pytest.fixture
    def client(self, store):
        s, *_ = store
        from fastapi.testclient import TestClient
        from bottleneck_hunter.web.decision_api import router, set_store
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router, prefix="/api/decision")
        set_store(s)
        return TestClient(app)

    def test_overview(self, client):
        resp = client.get("/api/decision/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "macro_strategy" in data
        assert "tactical_plans" in data
        assert "pending_executions" in data

    def test_tactical_latest(self, client):
        resp = client.get("/api/decision/tactical/latest")
        assert resp.status_code == 200

    def test_tactical_by_ticker(self, client):
        resp = client.get("/api/decision/tactical/AAPL")
        assert resp.status_code == 200

    def test_pending_executions(self, client):
        resp = client.get("/api/decision/executions/pending")
        assert resp.status_code == 200

    def test_account(self, client):
        resp = client.get("/api/decision/account")
        assert resp.status_code == 200
        data = resp.json()
        assert "account" in data

    def test_committee_reviews_empty(self, client):
        resp = client.get("/api/decision/committee/reviews/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["reviews"] == []

    def test_committee_consensus_empty(self, client):
        resp = client.get("/api/decision/committee/consensus/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["consensus"] is None

    def test_execution_detail_404(self, client):
        resp = client.get("/api/decision/execution/nonexistent")
        assert resp.status_code == 404
