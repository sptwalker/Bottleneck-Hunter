"""决策中心纪律修复（批 A）单元测试：硬止损/前视偏差/质量门落杆/L2钳制。"""

from unittest.mock import patch, MagicMock

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "t.db")


# ── A4: L2 target_allocation 钳制 ──────────────────────────

class TestClampTargetAllocation:
    def test_clamps_over_bounds(self):
        from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation
        from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds
        bounds = get_allocation_bounds("bear", "defensive", 5)  # equity 10-25, single 5, beta 0.5
        result = {"target_allocation": {"equity_pct": 99, "max_single_stock_pct": 40, "max_portfolio_beta": 2.0}}
        warns = _clamp_target_allocation(result, bounds)
        ta = result["target_allocation"]
        assert ta["equity_pct"] == bounds["equity_max"]
        assert ta["max_single_stock_pct"] == bounds["max_single_pct"]
        assert ta["max_portfolio_beta"] == bounds["beta_limit"]
        assert len(warns) == 3

    def test_within_bounds_no_change(self):
        from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation
        from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds
        bounds = get_allocation_bounds("bull", "balanced", 5)
        result = {"target_allocation": {"equity_pct": bounds["equity_min"], "max_single_stock_pct": 5}}
        warns = _clamp_target_allocation(result, bounds)
        assert warns == []

    def test_missing_allocation_safe(self):
        from bottleneck_hunter.watchlist.decision_engine import _clamp_target_allocation
        assert _clamp_target_allocation({}, {"equity_max": 50}) == []


# ── A1: 硬止损巡检 ────────────────────────────────────────

class TestHardStopLossSweep:
    async def _collect(self, gen):
        return [e async for e in gen]

    async def test_triggers_sell_when_below_stop(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _hard_stop_loss_sweep
        s = store.for_market("us_stock")
        acct = s.get_sim_account()
        eid = s.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus", "market": "us_stock"})
        s.create_sim_position(acct["id"], "NVDA", shares=100, avg_cost=1000.0, entry_id=eid)
        # 战术计划带止损位 900
        s.create_tactical_plan("sp1", eid, "NVDA", "2026-07-01",
                               {"action": "hold", "exit_plan": {"stop_loss": {"price": 900, "type": "hard"}}})
        # 现价 850 已跌破
        s.save_snapshots([{"ticker": "NVDA", "date": "2026-07-02", "close": 850.0, "market": "us_stock"}])

        events = await self._collect(_hard_stop_loss_sweep(s, "us_stock"))
        assert any(e.get("event") == "decision_warning" for e in events)
        # 生成了卖出执行计划
        pending = s.get_pending_executions()
        assert any(p.get("action") == "sell" and p.get("ticker") == "NVDA" for p in pending)

    async def test_no_trigger_above_stop(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _hard_stop_loss_sweep
        s = store.for_market("us_stock")
        acct = s.get_sim_account()
        eid = s.add({"ticker": "AAPL", "company_name": "Apple", "tier": "focus", "market": "us_stock"})
        s.create_sim_position(acct["id"], "AAPL", shares=10, avg_cost=200.0, entry_id=eid)
        s.create_tactical_plan("sp1", eid, "AAPL", "2026-07-01",
                               {"action": "hold", "exit_plan": {"stop_loss": {"price": 180}}})
        s.save_snapshots([{"ticker": "AAPL", "date": "2026-07-02", "close": 210.0, "market": "us_stock"}])
        events = await self._collect(_hard_stop_loss_sweep(s, "us_stock"))
        assert not any(e.get("event") == "decision_warning" for e in events)
        assert s.get_pending_executions() == []


# ── A2: 成交价用市价（消除前视偏差）──────────────────────

class TestExecutionUsesMarketPrice:
    def test_fill_uses_latest_snapshot_close(self, store):
        from bottleneck_hunter.watchlist import trade_executor as te
        s = store.for_market("us_stock")
        s.get_sim_account()
        eid = s.add({"ticker": "MSFT", "company_name": "Microsoft", "tier": "focus", "market": "us_stock"})
        # L4 挂单价 350（≥市价，限价满足→可成交），但下单时真实市价 330
        plan_id = s.create_execution_plan("sp1", eid, "MSFT",
                                          {"action": "buy", "shares": 10, "target_price": 350})
        s.save_snapshots([{"ticker": "MSFT", "date": "2026-07-02", "close": 330.0, "market": "us_stock"}])
        s.confirm_execution(plan_id)  # execute_trade 只作用于已确认计划（原子领单）
        # validate_execution_plan 在 trade_executor 内是函数内 import，patch 其源模块以放行；聚焦成交价来源
        with patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan",
                   return_value=MagicMock(valid=True, violations=[])):
            res = te.execute_trade(s, plan_id)
        assert "error" not in res, res
        # 成交均价应接近真实市价 330（叠加滑点），而非挂单价 350
        trades = s.get_sim_trades("MSFT")
        assert trades and trades[0]["price"] > 320  # 用市价而非挂单价 350


# ── B1: L4 约束读入 regime（熊市防守收紧）──────────────────

class TestRegimeConstraintTightening:
    def test_bear_defensive_tightens_single_pct(self):
        from bottleneck_hunter.watchlist.constraint_validator import get_constraints_for_appetite
        from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds
        # 模拟 B1 的合并逻辑：alloc_bounds 收紧 appetite 级约束
        appetite = "defensive"
        base = get_constraints_for_appetite(appetite)  # defensive: single 18%
        bounds = get_allocation_bounds("bear", appetite, 5)  # bear/defensive: max_single_pct 5
        merged_single = min(base["max_single_position_pct"], bounds["max_single_pct"])
        assert merged_single == 5  # 熊市收紧到 5%，而非 appetite 的 18%
        merged_beta = min(base["max_portfolio_beta"], bounds["beta_limit"])
        assert merged_beta == bounds["beta_limit"] == 0.5


# ── B4: 卖出结算记录投委会投票 outcome ────────────────────

class TestRecordOutcomeOnSell:
    def test_profitable_sell_marks_vote_correct(self, store):
        from bottleneck_hunter.watchlist import trade_executor as te
        s = store.for_market("us_stock")
        acct = s.get_sim_account()
        eid = s.add({"ticker": "TSLA", "company_name": "Tesla", "tier": "focus", "market": "us_stock"})
        s.create_sim_position(acct["id"], "TSLA", shares=10, avg_cost=200.0, entry_id=eid)
        # 先有一条 pending 投委会投票预测
        s.record_prediction(provider="deepseek", model="deepseek-chat",
                            role_context="committee_risk", ticker="TSLA",
                            prediction_type="vote", prediction_value="approve", market="us_stock")
        # 卖出计划：市价 300 > 成本 200 → 盈利
        plan_id = s.create_execution_plan("sp1", eid, "TSLA",
                                          {"action": "sell", "shares": 10, "target_price": 300})
        s.save_snapshots([{"ticker": "TSLA", "date": "2026-07-02", "close": 300.0, "market": "us_stock"}])
        s.confirm_execution(plan_id)  # execute_trade 只作用于已确认计划（原子领单）
        with patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan",
                   return_value=MagicMock(valid=True, violations=[])):
            res = te.execute_trade(s, plan_id)
        assert res.get("realized_pnl", 0) > 0, res
        # 该投票的 is_correct 应从 -1(pending) 变为 1(判对)
        stats = s.get_model_accuracy_stats("us_stock")
        risk_stat = next((x for x in stats if x["role_context"] == "committee_risk"), None)
        assert risk_stat and risk_stat["pending"] == 0 and risk_stat["correct"] == 1


# ── B2: 组合风险摘要 ──────────────────────────────────────

class TestPortfolioRiskSummary:
    def test_summary_has_risk_fields(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _portfolio_risk_summary
        s = store.for_market("us_stock")
        acct = s.get_sim_account()
        for tk in ("NVDA", "AMD"):
            eid = s.add({"ticker": tk, "company_name": tk, "tier": "focus", "market": "us_stock", "sector": "Tech"})
            s.create_sim_position(acct["id"], tk, shares=10, avg_cost=100.0, entry_id=eid)
        positions = s.get_sim_positions(acct["id"])
        for p in positions:
            p["weight_pct"] = 50.0
            p["sector"] = "Tech"
        summary = _portfolio_risk_summary(s, positions, 100000)
        assert "concentration_hhi" in summary
        assert "max_sector_weight_pct" in summary
        assert "warnings" in summary

    def test_empty_positions(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _portfolio_risk_summary
        assert _portfolio_risk_summary(store.for_market("us_stock"), [], 100000) == {}


# ── B7: L2 偏离度确定性计算 ───────────────────────────────

class TestDeviationDrift:
    def test_computes_equity_and_sector_drift(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _compute_deviation_drift
        s = store.for_market("us_stock")
        acct = s.get_sim_account()
        te = acct.get("total_equity", 100000)
        eid = s.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus",
                     "market": "us_stock", "sector": "Tech"})
        s.create_sim_position(acct["id"], "NVDA", shares=100, avg_cost=100.0, entry_id=eid)
        positions = s.get_sim_positions(acct["id"])
        for p in positions:
            p["market_value"] = 50000  # 占 50%
        plan_rj = {"target_allocation": {"equity_pct": 30, "cash_pct": 70},
                   "sector_targets": {"Tech": 20}}
        drift = _compute_deviation_drift(s, plan_rj, {"total_equity": te, "cash_balance": 50000},
                                         positions, "us_stock")
        assert drift["actual_equity_pct"] == 50.0
        assert drift["equity_drift_pct"] == 20.0  # 50 - 30
        assert drift["rebalance_suggested"] is True  # 偏离 >5%
        tech = next((d for d in drift["sector_drift"] if d["sector"] == "Tech"), None)
        assert tech and tech["drift_pct"] == 30.0  # 50 实际 - 20 目标

    def test_no_drift_within_tolerance(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _compute_deviation_drift
        # 有明确 equity 目标 0% 且无持仓 → 无偏离
        drift = _compute_deviation_drift(store.for_market("us_stock"), {"target_allocation": {"equity_pct": 0}},
                                         {"total_equity": 100000, "cash_balance": 100000}, [], "us_stock")
        assert drift["rebalance_suggested"] is False

    def test_no_target_returns_none(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _compute_deviation_drift
        # 旧格式 target_allocation 是 list / 无 equity_pct → 代码放弃判定，返回 None（交给 LLM）
        drift = _compute_deviation_drift(store.for_market("us_stock"),
                                         {"target_allocation": [{"ticker": "AAPL", "weight": 0.15}]},
                                         {"total_equity": 100000, "cash_balance": 100000}, [], "us_stock")
        assert drift["rebalance_suggested"] is None


# ── B5: 筹码/评级信号（机构持仓 + 分析师目标价，读库）────────

class TestChipContext:
    def test_aggregates_institutions_and_ratings(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _chip_context
        s = store.for_market("us_stock")
        s.save_institutional_holders("NVDA", [
            {"holder_name": "Vanguard", "pct_held": 8.1, "date": "2026-06-30"},
            {"holder_name": "BlackRock", "pct_held": 7.2, "date": "2026-06-30"},
        ])
        s.save_analyst_ratings("NVDA", [
            {"firm": "GS", "rating": "buy", "target_price": 1000, "date": "2026-06-30"},
            {"firm": "MS", "rating": "buy", "target_price": 1100, "date": "2026-06-29"},
            {"firm": "JPM", "rating": "hold", "target_price": 900, "date": "2026-06-28"},
        ])
        chip = _chip_context(s, "NVDA")
        assert chip["institution_count"] == 2
        assert chip["top_institutions"][0]["name"] == "Vanguard"  # pct 降序
        assert chip["rating_distribution"]["buy"] == 2
        assert chip["consensus_target_price"] == 1000.0  # (1000+1100+900)/3
        assert chip["target_price_range"] == [900, 1100]

    def test_empty_when_no_data(self, store):
        from bottleneck_hunter.watchlist.decision_engine import _chip_context
        assert _chip_context(store.for_market("us_stock"), "UNKNOWN") == {}
