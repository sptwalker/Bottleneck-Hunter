"""Phase 9A 绩效报告与调优系统测试"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def perf_store(tmp_path):
    """带测试数据的 WatchlistStore"""
    store = WatchlistStore(tmp_path / "test.db")

    # 创建模拟账户
    account = store.get_sim_account()
    aid = account["id"]

    # 添加交易记录
    store.create_sim_trade(aid, "AAPL", "buy", 100, 150.0, 15000.0, reasoning="test buy")
    store.create_sim_trade(aid, "AAPL", "sell", 100, 160.0, 16000.0, reasoning="test sell")
    store.create_sim_trade(aid, "NVDA", "buy", 50, 400.0, 20000.0, reasoning="test buy 2")
    store.create_sim_trade(aid, "NVDA", "sell", 50, 380.0, 19000.0, reasoning="test sell 2")

    # 添加复盘记录
    trade_id = store.create_sim_trade(aid, "TSLA", "sell", 100, 220.0, 22000.0)
    store.create_auto_review(
        sim_trade_id=trade_id,
        ticker="TSLA",
        entry_price=200.0,
        exit_price=220.0,
        return_pct=10.0,
        result_json={"trade_quality_score": 8, "key_lessons": ["止盈及时", "顺势而为"]},
    )

    store.record_llm_usage({
        "provider": "deepseek",
        "model": "deepseek-chat",
        "input_tokens": 1000,
        "output_tokens": 500,
        "estimated_cost_usd": 0.15,
        "task_type": "test",
    })

    return store


# ─────────────────────────────────────────────────────────
# PerformanceCalculator 测试
# ─────────────────────────────────────────────────────────

def test_compute_overview_with_trades(perf_store):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)
    overview = calc.compute_overview()

    assert overview["total_trades"] >= 1
    assert 0 <= overview["win_rate"] <= 100
    assert "avg_return_pct" in overview
    assert "best_trade_pct" in overview
    assert "worst_trade_pct" in overview


def test_compute_overview_empty(tmp_path):
    store = WatchlistStore(tmp_path / "empty.db")
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(store)
    overview = calc.compute_overview()

    assert overview["total_trades"] == 0
    assert overview["win_rate"] == 0.0
    assert overview["avg_return_pct"] == 0.0


def test_compute_monthly_series(perf_store):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)
    monthly = calc.compute_monthly_series(months=3)

    assert isinstance(monthly, list)
    for m in monthly:
        assert "month" in m
        assert "trades" in m
        assert "win_rate" in m


def test_compute_drawdown(perf_store):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)
    dd = calc.compute_drawdown()

    assert "max_drawdown_pct" in dd
    assert dd["max_drawdown_pct"] >= 0
    assert "peak_date" in dd
    assert "trough_date" in dd


def test_compute_by_ticker(perf_store):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)
    tickers = calc.compute_by_ticker()

    assert isinstance(tickers, list)
    if tickers:
        assert "ticker" in tickers[0]
        assert "trades" in tickers[0]
        assert "win_rate" in tickers[0]


def test_compute_cost_summary(perf_store):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)
    cost = calc.compute_cost_summary()

    assert "daily_cost" in cost
    assert "monthly_cost" in cost
    assert "daily_limit" in cost
    assert "monthly_limit" in cost
    assert cost["daily_cost"] >= 0


# ─────────────────────────────────────────────────────────
# tuning_log CRUD 测试
# ─────────────────────────────────────────────────────────

def test_create_tuning_proposal(tmp_path):
    store = WatchlistStore(tmp_path / "tuning.db")

    tid = store.create_tuning_proposal(
        type_="weight",
        parameter_name="催化剂权重",
        old_value="0.15",
        new_value="0.10",
        reason="准确率不足",
        evidence=["案例1", "案例2"],
    )

    assert tid
    proposals = store.get_tuning_proposals()
    assert len(proposals) == 1
    assert proposals[0]["parameter_name"] == "催化剂权重"
    assert proposals[0]["status"] == "proposed"


def test_approve_tuning(tmp_path):
    store = WatchlistStore(tmp_path / "tuning.db")
    tid = store.create_tuning_proposal("weight", "测试参数", "1", "2", "测试")

    ok = store.approve_tuning(tid)
    assert ok

    proposals = store.get_tuning_proposals(status="approved")
    assert len(proposals) == 1
    assert proposals[0]["id"] == tid
    assert proposals[0]["decided_at"] is not None


def test_reject_tuning(tmp_path):
    store = WatchlistStore(tmp_path / "tuning.db")
    tid = store.create_tuning_proposal("threshold", "测试阈值", "0.8", "0.7", "测试")

    ok = store.reject_tuning(tid, "不合理")
    assert ok

    proposals = store.get_tuning_proposals(status="rejected")
    assert len(proposals) == 1


def test_get_proposals_filter(tmp_path):
    store = WatchlistStore(tmp_path / "tuning.db")

    t1 = store.create_tuning_proposal("weight", "参数1", "1", "2", "理由1")
    t2 = store.create_tuning_proposal("threshold", "参数2", "3", "4", "理由2")
    store.approve_tuning(t1)

    proposed = store.get_tuning_proposals(status="proposed")
    assert len(proposed) == 1
    assert proposed[0]["id"] == t2

    approved = store.get_tuning_proposals(status="approved")
    assert len(approved) == 1
    assert approved[0]["id"] == t1


# ─────────────────────────────────────────────────────────
# API 端点测试
# ─────────────────────────────────────────────────────────

def test_performance_endpoint(perf_store):
    """测试 /performance 端点返回格式"""
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)

    # 模拟 API 返回结构
    result = {
        "overview": calc.compute_overview(),
        "drawdown": calc.compute_drawdown(),
        "cost": calc.compute_cost_summary(),
    }

    assert "overview" in result
    assert "drawdown" in result
    assert "cost" in result
    assert result["overview"]["total_trades"] >= 0


def test_performance_monthly(perf_store):
    """测试 /performance/monthly 端点"""
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(perf_store)

    monthly = calc.compute_monthly_series(months=6)
    result = {"monthly": monthly}

    assert "monthly" in result
    assert isinstance(result["monthly"], list)


def test_tuning_crud_api(tmp_path):
    """测试调优 CRUD API 流程"""
    store = WatchlistStore(tmp_path / "api.db")

    # 创建
    tid = store.create_tuning_proposal("rule", "测试规则", "旧", "新", "原因")

    # 查询
    proposals = store.get_tuning_proposals()
    assert len(proposals) == 1

    # 批准
    ok = store.approve_tuning(tid)
    assert ok

    # 验证状态
    approved = store.get_tuning_proposals(status="approved")
    assert len(approved) == 1


# ─────────────────────────────────────────────────────────
# 调优引擎测试
# ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_tuning_no_reviews(tmp_path):
    """无复盘记录时跳过调优"""
    store = WatchlistStore(tmp_path / "no_reviews.db")

    from bottleneck_hunter.watchlist.tuning_engine import generate_tuning_suggestions

    events = []
    async for evt in generate_tuning_suggestions(store, budget=None):
        events.append(evt)

    assert any("不足" in evt.get("data", {}).get("message", "") for evt in events)


@pytest.mark.asyncio
async def test_generate_tuning_success(perf_store):
    """成功生成调优建议（mock LLM）"""

    # 添加更多复盘数据
    aid = perf_store.get_sim_account()["id"]
    for i in range(5):
        tid = perf_store.create_sim_trade(aid, f"TEST{i}", "sell", 100, 100.0, 10000.0)
        perf_store.create_auto_review(
            sim_trade_id=tid,
            ticker=f"TEST{i}",
            entry_price=100.0,
            exit_price=95.0,
            return_pct=-5.0,
            result_json={"trade_quality_score": 5, "key_lessons": ["止损不及时"]},
        )

    mock_response = MagicMock()
    mock_response.content = json.dumps({
        "analysis": "系统性偏差测试",
        "suggestions": [{
            "type": "weight",
            "parameter": "测试参数",
            "current": "1.0",
            "suggested": "0.8",
            "reason": "测试理由",
            "evidence": ["证据1"],
        }],
    }, ensure_ascii=False)

    mock_llm = MagicMock()
    mock_llm.invoke.return_value = mock_response

    from bottleneck_hunter.watchlist.tuning_engine import generate_tuning_suggestions

    with patch("bottleneck_hunter.watchlist.tuning_engine.get_llm_for_position", return_value=(mock_llm, "test", "test")):
        events = []
        async for evt in generate_tuning_suggestions(perf_store, budget=None):
            events.append(evt)

        # 验证生成成功
        done_events = [e for e in events if e.get("event") == "tuning_done"]
        assert len(done_events) > 0

        # 验证建议已写入数据库
        proposals = perf_store.get_tuning_proposals()
        assert len(proposals) >= 1
