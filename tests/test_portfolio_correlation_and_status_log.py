"""P2.1 组合相关性约束 + P2.2 执行状态机留痕 回归测试。

两份病史：
- P2.1：`docs/DECISION_LOOP_IMPROVEMENT.md` 声称 P2.1 三子项已全部完成（组合 beta / 行业集中 /
  **相关性集中**），但 `constraint_validator.py` 里 `correlation` 零命中——报告层
  `risk_metrics` 早就在算高相关对并写进 L2/投委会 prompt，生成期却从未因此拦过或提示过一笔，
  "避免买入与持仓高相关的标的"从来没生效。本测试盯住这条通道真的接通了。
- P2.2：`execution_plans.status` 是唯一真相，14 个变迁方法全是裸 UPDATE，事后只看得见终态。
  最阴的是 `revert_to_pending`：失败回滚后的行与「刚生成还没轮到」逐字节相同。
"""

from __future__ import annotations

import random

import pytest

from bottleneck_hunter.watchlist.constraint_validator import (
    DEFAULT_CONSTRAINTS,
    REGIME_CONSTRAINTS,
    get_constraints_for_appetite,
    validate_portfolio_correlation,
)
from bottleneck_hunter.watchlist.risk_metrics import compute_portfolio_risk, high_correlation_pairs
from bottleneck_hunter.watchlist.store import WatchlistStore

C = {**DEFAULT_CONSTRAINTS}


def _prices(returns: list[float], start: float = 100.0) -> list[float]:
    out, px = [], start
    for r in returns:
        px *= 1 + r
        out.append(px)
    return out


def _correlated_pair(seed_a=1, seed_b=2, seed_shock=7, n=60, w=0.9):
    """两条 ρ≈0.95 的收盘价序：共用一根强冲击 + 各自弱噪声。

    不能用 "每天固定涨 1%" 造 —— 那样收益率是常数、std=0，ρ 根本无定义（_pearson 直接返 0），
    测试会以"看起来像功能坏了"的方式失败。
    """
    # Random 必须在推导式外建一次：写在推导式里等于每个元素都重播种，整列变成同一个常数,
    # 收益率又是常数 → 还是 std=0 的老坑。
    srng = random.Random(seed_shock)
    shock = [srng.gauss(0, 0.01) for _ in range(n)]   # 同一根序列 → 共享
    out = []
    for s in (seed_a, seed_b):
        rng = random.Random(s)
        out.append(_prices([w * shock[i] + (1 - w) * rng.gauss(0, 0.01) for i in range(n)]))
    return out


def _buy(ticker="NVDA", shares=100, price=100.0):
    return {"id": "p1", "action": "buy", "ticker": ticker, "shares": shares, "target_price": price,
            "result_json": {"action": "buy", "shares": shares, "target_price": price}}


class TestCorrelationHelper:
    def test_同涨同跌序列被判高相关(self):
        a, b = _correlated_pair()
        pairs = high_correlation_pairs({"A": a, "B": b})
        assert [p["ticker_a"] for p in pairs] == ["A"] and pairs[0]["ticker_b"] == "B"
        assert pairs[0]["correlation"] > 0.9

    def test_反向序列也判高相关_罚的是伪分散不是方向(self):
        """ρ=-0.9 的对冲对不该被罚——但「同涨同跌」是两头都成立的伪分散，故用 abs。

        注意不能用 `list(reversed(prices))` 造反向：倒序价序的收益率不是原收益率的相反数
        （r → -r/(1+r)，且顺序也翻了），实测 ρ≈0，根本进不了这条断言。要的是收益率取负。
        """
        a, _ = _correlated_pair()
        ra = [a[i] / a[i - 1] - 1 for i in range(1, len(a))]
        # 前导的 0.0 是占位：a[0] 是起始价、没有"前一天"，缺了它整条序列就错位一天，ρ 掉到 0
        b = _prices([0.0] + [-r for r in ra], start=a[0])
        pairs = high_correlation_pairs({"A": a, "B": b})
        assert pairs and pairs[0]["correlation"] < -0.99

    def test_样本不足不报(self):
        """19 个交易日 < min_samples 20：两三天的巧合能轻松到 0.9，报了就是天天误报。"""
        a, b = _correlated_pair(n=19)
        assert high_correlation_pairs({"A": a, "B": b}) == []

    def test_脏价整条跳过不崩(self):
        a, _ = _correlated_pair()
        assert high_correlation_pairs({"A": [None, "", "x"], "B": a}) == []
        assert high_correlation_pairs({"A": [0.0, 0.0], "B": a}) == []

    def test_报告路径仍产出高相关对(self):
        """报告层（L2/VIP/投委会读的 high_correlation_pairs）行为不得回退。"""
        a, b = _correlated_pair()
        m = compute_portfolio_risk(
            [{"ticker": "A", "weight_pct": 50.0}, {"ticker": "B", "weight_pct": 50.0}],
            {"A": a, "B": b},
        )
        assert len(m.correlation_pairs) == 1
        assert any("高相关" in w for w in m.warnings)


class TestValidatePortfolioCorrelation:
    def test_与持仓高相关则警告_但可用(self):
        a, b = _correlated_pair()
        r = validate_portfolio_correlation(
            _buy("NVDA"), [{"ticker": "AMD", "market_value": 10000}], {"NVDA": a, "AMD": b}, C,
        )
        assert r.valid, "相关性警告绝不能否决计划——相关性无法靠缩量降级，硬拦会拦死且无豁免通道"
        assert r.violations == [] and len(r.warnings) == 1
        assert "AMD" in r.warnings[0] and "ρ=" in r.warnings[0]

    def test_低相关不报(self):
        """错相位摆动的两条序列 → ρ 近零，即便摆动幅度很大也不是伪分散。"""
        import math
        a = [100.0 + 10 * math.sin(i) for i in range(60)]
        b = [100.0 + 10 * math.cos(i * 0.7) for i in range(60)]
        r = validate_portfolio_correlation(
            _buy("NVDA"), [{"ticker": "AMD", "market_value": 10000}], {"NVDA": a, "AMD": b}, C,
        )
        assert r.warnings == []

    def test_卖出不判相关性(self):
        """卖/减是在降低相关性敞口，报警只是噪声。"""
        a, b = _correlated_pair()
        p = _buy("NVDA")
        p["action"] = "sell"
        r = validate_portfolio_correlation(
            p, [{"ticker": "AMD", "market_value": 10000}], {"NVDA": a, "AMD": b}, C,
        )
        assert r.warnings == []

    def test_缺持仓不判(self):
        a, _ = _correlated_pair()
        r = validate_portfolio_correlation(_buy("NVDA"), [], {"NVDA": a}, C)
        assert r.warnings == []

    def test_缺本笔历史优雅降级只warning(self):
        """与 validate_portfolio_beta 的缺 beta 同构：数据不全时明说，不静默通过。"""
        _, b = _correlated_pair()
        r = validate_portfolio_correlation(
            _buy("NVDA"), [{"ticker": "AMD", "market_value": 10000}], {"AMD": b}, C,
        )
        assert r.valid and any("缺少本笔标的的历史价" in w for w in r.warnings)

    def test_阈值可关闭(self):
        a, b = _correlated_pair()
        r = validate_portfolio_correlation(
            _buy("NVDA"), [{"ticker": "AMD", "market_value": 10000}], {"NVDA": a, "AMD": b},
            {**C, "max_pair_correlation": 0},
        )
        assert r.warnings == []

    @pytest.mark.parametrize("appetite,expected", [("defensive", 0.75), ("balanced", 0.85), ("aggressive", 0.90)])
    def test_三档regime阈值逐档放宽(self, appetite, expected):
        assert get_constraints_for_appetite(appetite)["max_pair_correlation"] == expected

    def test_默认约束含该键(self):
        """缺键 → threshold 为 None → 静默返回空结果，约束变空转。"""
        assert "max_pair_correlation" in DEFAULT_CONSTRAINTS
        assert all("max_pair_correlation" in v for v in REGIME_CONSTRAINTS.values())


class TestExecutionStatusLog:
    """P2.2：每个状态变迁都必须留痕，且与状态变更同事务。"""

    @pytest.fixture()
    def store(self, tmp_path):
        return WatchlistStore(db_path=tmp_path / "p22.db").for_user("u").for_market("us_stock")

    def test_创建计划留痕(self, store):
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        pid = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10},
                                          strict=False)
        log = store.get_execution_status_log(pid)
        assert len(log) == 1 and log[0]["from_status"] == "" and log[0]["to_status"] == "pending"
        assert log[0]["ticker"] == "AAPL" and log[0]["actor"] == "system"

    def test_确认_成交_两跳都留痕(self, store):
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        pid = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10},
                                          strict=False)
        assert store.confirm_execution(pid)
        assert store.mark_executed(pid)
        hops = [(r["from_status"], r["to_status"]) for r in reversed(store.get_execution_status_log(pid))]
        assert hops == [("", "pending"), ("pending", "confirmed"), ("confirmed", "executed")]

    def test_回滚留下的行与未轮到的行不再相同(self, store):
        """本功能存在的理由：失败回滚后 status 又是 pending，只有日志能区分。"""
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        a = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        b = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        store.confirm_execution(a)
        store.revert_to_pending(a)
        # 两张票的 execution_plans 行状态一致（都是 pending）
        assert store.get_execution_plan(a)["status"] == store.get_execution_plan(b)["status"]
        # 但日志不同：a 有过一次 confirmed→pending 的回滚，b 没有
        rb = [(r["from_status"], r["to_status"]) for r in store.get_execution_status_log(a)]
        assert ("confirmed", "pending") in rb
        assert all(r["to_status"] != "pending" or r["from_status"] == "" for r in
                   store.get_execution_status_log(b)[1:])

    def test_批量清空一票一条(self, store):
        """「清空」原本是唯一无痕的状态跃迁。"""
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        ids = [store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
               for _ in range(3)]
        assert store.clear_pending_executions() == 3
        for pid in ids:
            log = store.get_execution_status_log(pid)
            assert any(r["to_status"] == "rejected" and r["actor"] == "user" for r in log)

    def test_空清空不写日志(self, store):
        assert store.clear_pending_executions() == 0
        assert store.get_recent_exec_status_log() == []

    def test_多用户隔离(self, tmp_path):
        """留痕必须按 user 隔离——这正是不能用 SQLite 触发器的原因（触发器拿不到 user_id）。"""
        db = tmp_path / "iso.db"
        u1 = WatchlistStore(db_path=db).for_user("u1").for_market("us_stock")
        u2 = WatchlistStore(db_path=db).for_user("u2").for_market("us_stock")
        eid = u1.add({"ticker": "AAPL", "market": "us_stock"})
        pid = u1.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        assert len(u1.get_execution_status_log(pid)) == 1
        assert u2.get_execution_status_log(pid) == []
        assert u2.get_recent_exec_status_log() == []

    def test_多市场隔离(self, tmp_path):
        db = tmp_path / "mkt.db"
        us = WatchlistStore(db_path=db).for_user("u").for_market("us_stock")
        cn = WatchlistStore(db_path=db).for_user("u").for_market("a_stock")
        eid = us.add({"ticker": "AAPL", "market": "us_stock"})
        pid = us.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        assert cn.get_execution_status_log(pid) == []

    def test_用户否决记actor为user(self, store):
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        pid = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        store.reject_execution(pid, "不喜欢这个价位")
        log = store.get_execution_status_log(pid)
        assert log[0]["to_status"] == "rejected" and log[0]["actor"] == "user"
        assert log[0]["reason"] == "不喜欢这个价位"

    def test_投委会否决记actor为committee(self, store):
        eid = store.add({"ticker": "AAPL", "market": "us_stock"})
        pid = store.create_execution_plan("", eid, "AAPL", {"action": "buy", "shares": 10}, strict=False)
        store.reject_execution(pid, f"{store.BLOCK_MARKER_COMMITTEE} 集中度过高")
        assert store.get_execution_status_log(pid)[0]["actor"] == "committee"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
