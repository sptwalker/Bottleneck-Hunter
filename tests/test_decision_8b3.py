"""Tests for 8B.3 — 交易执行引擎、持仓 CRUD、API 交易闭环"""

import pytest
from bottleneck_hunter.watchlist.store import WatchlistStore


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

    macro_id = s.create_macro_strategy({"market_summary": "市场偏多"})
    strat_id = s.create_strategic_plan(macro_id, {
        "target_allocation": [{"ticker": "AAPL", "weight": 0.15, "action": "buy"}],
    })

    # execute_trade 现要求真实价快照（拒绝按 LLM 定价成交）；给 AAPL 一条收盘价快照
    s.save_snapshots([{"ticker": "AAPL", "date": "2026-07-14", "close": 188.0}])

    return s, entry_id, macro_id, strat_id


# ─────────────────────────────────────────────────────────
# Store Position CRUD
# ─────────────────────────────────────────────────────────

class TestStorePositionCRUD:
    def test_create_and_get_position(self, store):
        s, entry_id, *_ = store
        account = s.get_sim_account()
        pid = s.create_sim_position(account["id"], "AAPL", 100, 188.0, entry_id)
        assert pid

        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos is not None
        assert pos["ticker"] == "AAPL"
        assert pos["shares"] == 100
        assert pos["avg_cost"] == 188.0

    def test_update_position(self, store):
        s, *_ = store
        account = s.get_sim_account()
        pid = s.create_sim_position(account["id"], "AAPL", 50, 190.0)

        ok = s.update_sim_position(pid, shares=100, avg_cost=185.0, market_value=18500.0)
        assert ok is True

        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos["shares"] == 100
        assert pos["avg_cost"] == 185.0
        assert pos["market_value"] == 18500.0

    def test_delete_position(self, store):
        s, *_ = store
        account = s.get_sim_account()
        pid = s.create_sim_position(account["id"], "AAPL", 50, 190.0)

        ok = s.delete_sim_position(pid)
        assert ok is True

        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos is None

    def test_get_nonexistent_position(self, store):
        s, *_ = store
        account = s.get_sim_account()
        pos = s.get_sim_position(account["id"], "MSFT")
        assert pos is None


# ─────────────────────────────────────────────────────────
# Trade Executor
# ─────────────────────────────────────────────────────────

class TestTradeExecutor:
    def test_execute_buy(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 50, "target_price": 188.0,
            "amount": 9400, "reasoning": "分批建仓",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" not in result
        assert result["side"] == "buy"
        assert result["shares"] == 50
        # 成交价含滑点，允许小幅偏移（买入略高于目标价）
        assert 188.0 <= result["price"] <= 188.0 * 1.01

        account = s.get_sim_account()
        assert account["cash_balance"] < 100000

        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos is not None
        assert pos["shares"] == 50

    def test_execute_sell(self, store):
        s, entry_id, *_ = store
        account = s.get_sim_account()
        s.create_sim_position(account["id"], "AAPL", 100, 180.0, entry_id)

        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "sell", "shares": 50, "target_price": 180.0,
            "amount": 10000, "reasoning": "止盈",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" not in result
        assert result["side"] == "sell"
        assert result["realized_pnl"] > 0

        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos is not None
        assert pos["shares"] == 50

    def test_execute_sell_all(self, store):
        s, entry_id, *_ = store
        account = s.get_sim_account()
        s.create_sim_position(account["id"], "AAPL", 50, 180.0, entry_id)

        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "sell", "shares": 50, "target_price": 180.0,
            "amount": 10000, "reasoning": "清仓",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" not in result
        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos is None

    def test_insufficient_funds(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 10000, "target_price": 188.0,
            "amount": 1880000, "reasoning": "test",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" in result
        assert "约束校验不通过" in result["error"] or "现金不足" in result["error"]

    def test_insufficient_shares(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "sell", "shares": 100, "target_price": 200.0,
            "amount": 20000, "reasoning": "test",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" in result
        assert "约束校验不通过" in result["error"] or "持仓不足" in result["error"]

    def test_buy_adds_to_existing_position(self, store):
        s, entry_id, *_ = store
        account = s.get_sim_account()
        s.create_sim_position(account["id"], "AAPL", 50, 180.0, entry_id)

        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "add", "shares": 50, "target_price": 190.0,
            "amount": 9500, "reasoning": "加仓",
        })
        s.confirm_execution(plan_id)
        s.save_snapshots([{"ticker": "AAPL", "date": "2026-07-14", "close": 190.0}])  # 加仓价快照

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        result = execute_trade(s, plan_id)

        assert "error" not in result
        pos = s.get_sim_position(account["id"], "AAPL")
        assert pos["shares"] == 100
        # 加仓均价含滑点：(50*180 + 50*~190.x)/100，约 185，允许滑点偏移
        assert 185.0 <= pos["avg_cost"] <= 185.0 * 1.01

    def test_account_recalculated(self, store):
        s, entry_id, *_ = store
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 50, "target_price": 188.0,
            "amount": 9400, "reasoning": "test",
        })
        s.confirm_execution(plan_id)

        from bottleneck_hunter.watchlist.trade_executor import execute_trade
        execute_trade(s, plan_id)

        account = s.get_sim_account()
        assert account["total_trades"] == 1
        assert account["total_equity"] > 0


# ─────────────────────────────────────────────────────────
# Decision API — 交易闭环
# ─────────────────────────────────────────────────────────

class TestDecisionAPITrade:
    @pytest.fixture
    def client(self, store):
        s, *_ = store
        from fastapi.testclient import TestClient
        from bottleneck_hunter.web.decision_api import router as dc_router, set_store as dc_set_store
        from bottleneck_hunter.web.trading_api import router as tr_router, set_store as tr_set_store
        from fastapi import FastAPI

        app = FastAPI()
        # confirm/reject 在 decision_api；account/equity-history 在 trading_api，两者都挂
        app.include_router(dc_router, prefix="/api/decision")
        app.include_router(tr_router, prefix="/api/trading")
        dc_set_store(s)
        tr_set_store(s)
        from bottleneck_hunter.auth.dependencies import get_current_user
        app.dependency_overrides[get_current_user] = lambda: {"sub": "", "username": "test", "role": "admin"}
        return TestClient(app), s

    def test_confirm_triggers_trade(self, client, store):
        c, s = client
        entry_id = store[1]
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 10, "target_price": 188.0,
            "amount": 1880, "reasoning": "test",
        })

        resp = c.post(f"/api/decision/executions/{plan_id}/confirm")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert "trade" in data
        assert data["trade"]["side"] == "buy"

    def test_reject_no_trade(self, client, store):
        c, s = client
        entry_id = store[1]
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 10, "target_price": 188.0,
        })

        resp = c.post(f"/api/decision/executions/{plan_id}/reject",
                       json={"reason": "风险过高"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        plan = s.get_execution_plan(plan_id)
        assert plan["status"] == "rejected"

    def test_equity_history_empty(self, client, store):
        c, s = client
        resp = c.get("/api/trading/account/equity-history")
        assert resp.status_code == 200
        assert resp.json()["history"] == []

    def test_equity_history_after_trade(self, client, store):
        c, s = client
        entry_id = store[1]
        plan_id = s.create_execution_plan("tac_1", entry_id, "AAPL", {
            "action": "buy", "shares": 10, "target_price": 188.0,
            "amount": 1880, "reasoning": "test",
        })
        c.post(f"/api/decision/executions/{plan_id}/confirm")

        resp = c.get("/api/trading/account/equity-history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) >= 1
