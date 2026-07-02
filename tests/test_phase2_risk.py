"""Phase 2 验收：账户级熔断 + 分行业瓶颈权重。

对应改进方案 2.5（账户级 stop-loss/circuit-breaker）、2.2（分行业权重级联）。
运行：pytest tests/test_phase2_risk.py -q
"""
from bottleneck_hunter.watchlist.constraint_validator import check_account_circuit_breaker
from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer, DEFAULT_WEIGHTS
from bottleneck_hunter.chain.models import BottleneckDimension as D


class TestCircuitBreaker:
    def test_not_tripped_at_peak(self):
        r = check_account_circuit_breaker(
            {"total_equity": 100000, "peak_equity": 100000, "initial_capital": 100000})
        assert r.valid

    def test_drawdown_trips(self):
        # 距峰值回撤 25% ≥ 20%
        r = check_account_circuit_breaker(
            {"total_equity": 75000, "peak_equity": 100000, "initial_capital": 100000})
        assert not r.valid
        assert "回撤" in r.violations[0]

    def test_daily_loss_trips(self):
        r = check_account_circuit_breaker(
            {"total_equity": 90000, "peak_equity": 90000}, today_start_equity=100000)
        assert not r.valid
        assert "单日" in r.violations[0]

    def test_small_daily_loss_ok(self):
        r = check_account_circuit_breaker(
            {"total_equity": 95000, "peak_equity": 95000}, today_start_equity=100000)
        assert r.valid

    def test_zero_equity_safe(self):
        # 无权益数据时不误触发
        r = check_account_circuit_breaker({"total_equity": 0})
        assert r.valid


class TestIndustryWeights:
    def test_semiconductor_weights_applied(self):
        a = BottleneckAnalyzer(llms=[(None, "x", "x")], industry="半导体")
        assert a.weights[D.TECH_BARRIER] == 0.30
        assert a.weights != DEFAULT_WEIGHTS

    def test_fuzzy_match(self):
        a = BottleneckAnalyzer(llms=[(None, "x", "x")], industry="消费电子")
        assert a.weights[D.PRICING_POWER] == 0.35

    def test_unknown_industry_falls_back(self):
        a = BottleneckAnalyzer(llms=[(None, "x", "x")], industry="某冷门行业")
        assert a.weights == DEFAULT_WEIGHTS

    def test_no_industry_default(self):
        a = BottleneckAnalyzer(llms=[(None, "x", "x")])
        assert a.weights == DEFAULT_WEIGHTS


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
