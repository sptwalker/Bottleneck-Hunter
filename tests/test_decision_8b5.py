"""Tests for 8B.5 — L1/L2 决策引擎、E2E 流程、BudgetTracker、数据收集"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock, AsyncMock

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.models import DegradationMode


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "test.db")
    s = WatchlistStore(db)

    entry_id = s.add({
        "ticker": "AAPL",
        "company_name": "Apple Inc",
        "market": "us_stock",
        "sector": "科技",
        "tier": "focus",
    })

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

    return s, entry_id


@pytest.fixture
def store_with_macro(store):
    s, entry_id = store
    macro_id = s.create_macro_strategy({
        "regime": "bullish",
        "risk_appetite": "moderate",
        "market_summary": "市场偏多",
        "sector_outlook": {"科技": "bullish"},
    })
    return s, entry_id, macro_id


@pytest.fixture
def store_with_strategic(store_with_macro):
    s, entry_id, macro_id = store_with_macro
    strat_id = s.create_strategic_plan(macro_id, {
        "overall_stance": "积极",
        "target_allocation": [
            {"ticker": "AAPL", "weight": 0.15, "action": "buy"}
        ],
        "cash_reserve": 0.3,
    })
    return s, entry_id, macro_id, strat_id


def _mock_llm_response(content: dict):
    llm = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(content, ensure_ascii=False)
    llm.invoke = MagicMock(return_value=msg)
    return llm


async def _collect_events(gen):
    events = []
    async for evt in gen:
        events.append(evt)
    return events


# ─────────────────────────────────────────────────────────
# L1 宏观策略
# ─────────────────────────────────────────────────────────

MACRO_RESPONSE = {
    "regime": "bullish",
    "risk_appetite": "moderate",
    "market_summary": "美股继续走强",
    "sector_outlook": {"科技": "bullish", "金融": "neutral"},
    "key_risks": ["通胀反弹", "地缘政治"],
}


class TestL1MacroStrategy:
    @pytest.mark.asyncio
    async def test_run_macro_strategy_success(self, store):
        s, _ = store
        llm = _mock_llm_response(MACRO_RESPONSE)
        from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_macro_strategy(s))

        event_types = [e["event"] for e in events]
        assert "decision_start" in event_types
        assert "decision_done" in event_types
        assert not any(e == "decision_error" for e in event_types)

        macro = s.get_latest_macro_strategy()
        assert macro is not None
        assert macro["result_json"]["regime"] == "bullish"

    @pytest.mark.asyncio
    async def test_run_macro_strategy_no_llm(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(None, "", "")):
            events = await _collect_events(run_macro_strategy(s))

        assert any(e["event"] == "decision_error" for e in events)
        assert s.get_latest_macro_strategy() is None

    @pytest.mark.asyncio
    async def test_run_macro_strategy_budget_blocked(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy

        budget = MagicMock()
        budget.can_spend = MagicMock(return_value=False)

        llm = _mock_llm_response(MACRO_RESPONSE)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_macro_strategy(s, budget))

        assert any(e["event"] == "decision_error" for e in events)
        llm.invoke.assert_not_called()

    @pytest.mark.slow  # 真实拉宏观数据(FRED/指数)，~8min；fast 子集用 -m "not slow" 排除
    @pytest.mark.asyncio
    async def test_run_macro_check_valid(self, store_with_macro):
        s, _, macro_id = store_with_macro
        from bottleneck_hunter.watchlist.decision_engine import run_macro_check

        check_response = {
            "strategy_status": "valid",
            "daily_commentary": "宏观环境无重大变化",
        }
        llm = _mock_llm_response(check_response)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_macro_check(s))

        done_events = [e for e in events if e["event"] == "decision_done"]
        assert len(done_events) == 1
        assert done_events[0]["data"]["status"] == "valid"

    @pytest.mark.asyncio
    async def test_run_macro_check_major_revision(self, store_with_macro):
        s, _, macro_id = store_with_macro
        from bottleneck_hunter.watchlist.decision_engine import run_macro_check

        call_count = {"n": 0}
        check_response = {
            "strategy_status": "needs_major_revision",
            "revision_reasons": ["市场大跌"],
        }

        def mock_get_llm(provider_hint=None, position=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_llm_response(check_response), "deepseek", "deepseek-chat"
            return _mock_llm_response(MACRO_RESPONSE), "deepseek", "deepseek-chat"

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=mock_get_llm):
            events = await _collect_events(run_macro_check(s))

        event_types = [e["event"] for e in events]
        assert "decision_info" in event_types
        done_events = [e for e in events if e["event"] == "decision_done"]
        assert any(e["data"].get("layer") == "L1" and e["data"].get("regime")
                   for e in done_events)

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_run_macro_check_no_existing(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import run_macro_check

        llm = _mock_llm_response(MACRO_RESPONSE)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_macro_check(s))

        assert any(e["event"] == "decision_info" for e in events)
        assert s.get_latest_macro_strategy() is not None


# ─────────────────────────────────────────────────────────
# L2 组合策略
# ─────────────────────────────────────────────────────────

STRATEGIC_RESPONSE = {
    "overall_stance": "积极偏多",
    "target_allocation": [
        {"ticker": "AAPL", "weight": 0.20, "action": "increase"},
    ],
    "cash_reserve": 0.25,
    "reasoning": "科技股势头良好",
}


class TestL2StrategicPlan:
    @pytest.mark.asyncio
    async def test_run_strategic_plan_success(self, store_with_macro):
        s, _, macro_id = store_with_macro
        from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan

        llm = _mock_llm_response(STRATEGIC_RESPONSE)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_strategic_plan(s))

        event_types = [e["event"] for e in events]
        assert "decision_done" in event_types

        plan = s.get_latest_strategic_plan()
        assert plan is not None
        assert plan["result_json"]["overall_stance"] == "积极偏多"

    @pytest.mark.asyncio
    async def test_run_strategic_plan_auto_l1(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan

        call_count = {"n": 0}

        def mock_get_llm(provider_hint=None, position=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _mock_llm_response(MACRO_RESPONSE), "deepseek", "deepseek-chat"
            return _mock_llm_response(STRATEGIC_RESPONSE), "deepseek", "deepseek-chat"

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=mock_get_llm):
            events = await _collect_events(run_strategic_plan(s))

        assert any(e["event"] == "decision_info" and "L1" in e["data"].get("message", "")
                   for e in events)
        assert s.get_latest_macro_strategy() is not None
        assert s.get_latest_strategic_plan() is not None

    @pytest.mark.asyncio
    async def test_run_strategic_plan_no_llm(self, store_with_macro):
        s, *_ = store_with_macro
        from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(None, "", "")):
            events = await _collect_events(run_strategic_plan(s))

        assert any(e["event"] == "decision_error" for e in events)

    @pytest.mark.asyncio
    async def test_run_deviation_check_normal(self, store_with_strategic):
        s, *_ = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_deviation_check

        deviation_response = {
            "rebalance_needed": False,
            "overall_deviation_pct": 2.5,
            "commentary": "偏离度在可接受范围内",
        }
        llm = _mock_llm_response(deviation_response)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_deviation_check(s))

        done = [e for e in events if e["event"] == "decision_done"]
        assert len(done) == 1
        assert done[0]["data"]["rebalance_needed"] is False

    @pytest.mark.asyncio
    async def test_run_deviation_check_rebalance(self, store_with_strategic):
        s, *_ = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_deviation_check

        deviation_response = {
            "rebalance_needed": True,
            "overall_deviation_pct": 15.0,
            "commentary": "持仓偏离目标过大",
            "suggested_trades": [{"ticker": "AAPL", "action": "buy", "weight_change": 5}],
        }
        llm = _mock_llm_response(deviation_response)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_deviation_check(s))

        done = [e for e in events if e["event"] == "decision_done"]
        assert len(done) == 1
        assert done[0]["data"]["rebalance_needed"] is True


# ─────────────────────────────────────────────────────────
# E2E 完整流程
# ─────────────────────────────────────────────────────────

TACTICAL_RESPONSE = {
    "tactical_plans": [
        {
            "ticker": "AAPL",
            "action": "buy",
            "entry_plan": {"price_range": [185, 190], "method": "limit"},
            "exit_plan": {"stop_loss": 175, "take_profit": 210},
            "confidence": 8,
            "reasoning": "估值合理",
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
            "urgency": "normal",
            "reasoning": "分批建仓",
        }
    ],
    "execution_summary": {"total_actions": 1, "total_amount": 9400},
}

REVIEW_RESPONSE = {
    "vote": "approve",
    "score": 8,
    "confidence": 7,
    "key_concerns": [],
    "strengths": ["估值合理"],
    "risks": [],
}

CONSENSUS_RESPONSE = {
    "final_verdict": "approved",
    "approval_rate": 100,
    "consensus_modifications": [],
    "summary": "全票通过",
}

MACRO_CHECK_VALID = {
    "strategy_status": "valid",
    "daily_commentary": "市场平稳",
}

DEVIATION_CHECK_OK = {
    "rebalance_needed": False,
    "overall_deviation_pct": 3,
    "commentary": "正常",
}


class TestE2EDecisionFlow:
    def _get_llm_sequence(self, responses):
        idx = {"n": 0}
        def mock_get_llm(provider_hint=None, position=None):
            i = min(idx["n"], len(responses) - 1)
            idx["n"] += 1
            return _mock_llm_response(responses[i]), "deepseek", "deepseek-chat"
        return mock_get_llm

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_run_daily_decision_full(self, store_with_strategic):
        s, entry_id, macro_id, strat_id = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_daily_decision

        responses = [
            MACRO_CHECK_VALID,        # L1 check
            DEVIATION_CHECK_OK,       # L2 deviation
            TACTICAL_RESPONSE,        # L3
            EXECUTION_RESPONSE,       # L4
            REVIEW_RESPONSE,          # committee member 1
            REVIEW_RESPONSE,          # committee member 2
            REVIEW_RESPONSE,          # committee member 3
            REVIEW_RESPONSE,          # committee member 4
            CONSENSUS_RESPONSE,       # consensus
        ]

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=self._get_llm_sequence(responses)):
            with patch("bottleneck_hunter.watchlist.committee.get_llm_for_position",
                       side_effect=self._get_llm_sequence(
                           [REVIEW_RESPONSE] * 4 + [CONSENSUS_RESPONSE])):
                events = await _collect_events(run_daily_decision(s))

        event_types = [e["event"] for e in events]
        assert event_types[0] == "daily_start"
        assert event_types[-1] == "daily_done"
        assert any("L1" in str(e.get("data", {}).get("layer", "")) for e in events)
        assert any("L3" in str(e.get("data", {}).get("layer", "")) for e in events)
        assert any("L4" in str(e.get("data", {}).get("layer", "")) for e in events)

    @pytest.mark.asyncio
    async def test_run_daily_decision_l1_only(self, store_with_strategic):
        s, *_ = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_daily_decision

        llm = _mock_llm_response(MACRO_CHECK_VALID)
        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "deepseek", "deepseek-chat")):
            events = await _collect_events(run_daily_decision(s, scope="l1"))

        event_types = [e["event"] for e in events]
        assert "daily_start" in event_types
        assert "daily_done" in event_types
        assert not any(e.get("data", {}).get("layer") == "L3" for e in events)
        assert not any(e.get("data", {}).get("layer") == "L4" for e in events)

    @pytest.mark.asyncio
    async def test_run_daily_decision_l3l4_only(self, store_with_strategic):
        s, *_ = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_daily_decision

        responses = [TACTICAL_RESPONSE, EXECUTION_RESPONSE]

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=self._get_llm_sequence(responses)):
            events = await _collect_events(run_daily_decision(s, scope="l3l4"))

        event_types = [e["event"] for e in events]
        assert "daily_start" in event_types
        assert not any(e.get("data", {}).get("layer") == "L1" for e in events)
        assert any(e.get("data", {}).get("layer") == "L3" for e in events)

    @pytest.mark.asyncio
    async def test_run_daily_decision_catalyst_check(self, store_with_strategic):
        s, entry_id, *_ = store_with_strategic
        from bottleneck_hunter.watchlist.decision_engine import run_daily_decision

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        s.create_catalyst(entry_id, "AAPL", "过期催化剂", "event",
                          expected_date=yesterday)

        responses = [MACRO_CHECK_VALID, DEVIATION_CHECK_OK,
                     TACTICAL_RESPONSE, EXECUTION_RESPONSE]

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=self._get_llm_sequence(responses)):
            events = await _collect_events(run_daily_decision(s))

        assert any(e["event"] == "catalyst_expired" for e in events)

    @pytest.mark.asyncio
    async def test_run_full_refresh(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import run_full_refresh

        responses = [
            MACRO_RESPONSE,       # L1 generate
            STRATEGIC_RESPONSE,   # L2 generate
            TACTICAL_RESPONSE,    # L3
            EXECUTION_RESPONSE,   # L4
            REVIEW_RESPONSE,      # committee members
            REVIEW_RESPONSE,
            REVIEW_RESPONSE,
            REVIEW_RESPONSE,
            CONSENSUS_RESPONSE,   # consensus
        ]

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   side_effect=self._get_llm_sequence(responses)):
            with patch("bottleneck_hunter.watchlist.committee.get_llm_for_position",
                       side_effect=self._get_llm_sequence(
                           [REVIEW_RESPONSE] * 4 + [CONSENSUS_RESPONSE])):
                events = await _collect_events(run_full_refresh(s))

        event_types = [e["event"] for e in events]
        assert "refresh_start" in event_types
        assert "refresh_done" in event_types
        assert s.get_latest_macro_strategy() is not None
        assert s.get_latest_strategic_plan() is not None


# ─────────────────────────────────────────────────────────
# BudgetTracker
# ─────────────────────────────────────────────────────────

class TestBudgetTracker:
    @pytest.fixture
    def budget_store(self, tmp_path):
        db = str(tmp_path / "budget.db")
        s = WatchlistStore(db)
        return s

    def test_degradation_full(self, budget_store):
        bt = BudgetTracker(budget_store)
        assert bt.get_degradation_mode() == DegradationMode.FULL

    def test_degradation_reduced(self, budget_store):
        bt = BudgetTracker(budget_store)
        bt.record("openai", "gpt-4o", 300_000, 100_000, "test")
        mode = bt.get_degradation_mode()
        assert mode == DegradationMode.REDUCED

    def test_degradation_minimal(self, budget_store):
        bt = BudgetTracker(budget_store)
        bt.record("openai", "gpt-4o", 500_000, 200_000, "test")
        bt.record("openai", "gpt-4o", 500_000, 200_000, "test2")
        mode = bt.get_degradation_mode()
        assert mode == DegradationMode.MINIMAL

    def test_can_spend_minimal_blocks(self, budget_store):
        bt = BudgetTracker(budget_store)
        bt.record("openai", "gpt-4o", 500_000, 200_000, "test")
        bt.record("openai", "gpt-4o", 500_000, 200_000, "test2")
        assert bt.can_spend() is False

    def test_record_and_status(self, budget_store):
        bt = BudgetTracker(budget_store)
        bt.record("deepseek", "deepseek-chat", 10000, 5000, "macro")

        status = bt.get_status()
        assert status["daily_cost"] > 0
        assert status["mode"] == "full"
        assert status["daily_input_tokens"] == 10000
        assert status["daily_output_tokens"] == 5000


# ─────────────────────────────────────────────────────────
# 数据收集辅助函数
# ─────────────────────────────────────────────────────────

class TestDataCollection:
    @pytest.mark.asyncio
    async def test_collect_market_context_with_data(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context

        # 去网络化：mock 真实宏观指数
        fake_macro = {"sp500": {"value": 5500, "change_pct": 0.5, "label": "标普500"},
                      "nasdaq": {"value": 18000, "change_pct": 1.0, "label": "纳指"}}
        with patch("bottleneck_hunter.watchlist.macro_data.fetch_macro_data",
                   new=AsyncMock(return_value=fake_macro)):
            ctx = await _collect_market_context(s)
        assert "indices" in ctx
        assert "sectors" in ctx
        assert "sentiment" in ctx
        # 新契约：真实指数 + watchlist_breadth 子键（原自选股均值不再冒充大盘）
        assert "sp500" in ctx["indices"]
        assert ctx["indices"]["watchlist_breadth"]["stocks_tracked"] == 1
        assert ctx["indices"]["watchlist_breadth"]["avg_change_pct"] == 1.5

    @pytest.mark.asyncio
    async def test_collect_market_context_empty(self, tmp_path):
        db = str(tmp_path / "empty.db")
        s = WatchlistStore(db)
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context

        # 新契约：空观察池也应拿到真实大盘指数（不再返回空 indices）；无自选股则无 watchlist_breadth
        fake_macro = {"sp500": {"value": 5500, "change_pct": 0.5, "label": "标普500"}}
        with patch("bottleneck_hunter.watchlist.macro_data.fetch_macro_data",
                   new=AsyncMock(return_value=fake_macro)):
            ctx = await _collect_market_context(s)
        assert ctx["indices"] == {"sp500": {"value": 5500, "change_pct": 0.5, "label": "标普500"}}
        assert ctx["sectors"] == {}

    def test_collect_watchlist_signals(self, store):
        s, _ = store
        from bottleneck_hunter.watchlist.decision_engine import _collect_watchlist_signals

        signals = _collect_watchlist_signals(s)
        assert len(signals) == 1
        assert signals[0]["ticker"] == "AAPL"
        assert "signal" in signals[0]
        assert "price" in signals[0]
