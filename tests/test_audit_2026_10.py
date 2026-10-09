"""2026-10 体检回归：减险不受否决 / 投票方向标注+作废 / 关注对称 / 配额闩锁 /
PBO·DSR·块自助·成本压测 / P0-3 证据 ID。"""

import numpy as np
import pytest

from bottleneck_hunter.data_provider import scheduler as dps
from bottleneck_hunter.vip.advice_review import _judge
from bottleneck_hunter.watchlist.auto_execute import _is_hard_stop
from bottleneck_hunter.watchlist.committee import _fallback_consensus, _is_risk_reducing
from bottleneck_hunter.watchlist.evaluation import (
    block_bootstrap_ci,
    deflated_sharpe,
    overfit_flags,
    pbo_cscv,
)
from bottleneck_hunter.watchlist.event_backtest import Bar, Order, cost_stress
from bottleneck_hunter.watchlist.evidence import UNGROUNDED_LABEL, EvidenceIndex, evidence_id
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(str(tmp_path / "audit.db")).for_user("u1").for_market("us_stock")


# —— 减险计划不受投委会否决 ——

def test_risk_reducing_detection():
    assert _is_risk_reducing({"action": "sell"})
    assert _is_risk_reducing({"action": "REDUCE"})
    assert _is_risk_reducing({"action": "buy", "_hard_stop": True})
    assert not _is_risk_reducing({"action": "buy"})
    assert not _is_risk_reducing(None)
    assert _is_hard_stop({"result_json": {"_hard_stop": True}})
    assert not _is_hard_stop({"result_json": "{}"})


# —— 投票结算：作废终态不计对错、按 prediction_value 与 market 过滤 ——

def test_record_outcome_void_and_value_filter(store):
    for v in ("approve", "reject", "abstain"):
        store.record_prediction(provider="p", model="m", role_context=f"committee_{v}", ticker="NVDA",
                                prediction_type="vote", prediction_value=v, market="us_stock")
    # 他市场同票不应被结算
    store.record_prediction(provider="p", model="m", role_context="committee_x", ticker="NVDA",
                            prediction_type="vote", prediction_value="approve", market="a_stock")
    assert store.record_outcome("NVDA", "vote", "win", score_delta=0.0,
                                prediction_values=("approve",), market="us_stock") == 1
    assert store.record_outcome("NVDA", "vote", "win", score_delta=3.0,
                                prediction_values=("reject",), market="us_stock") == 1
    assert store.record_outcome("NVDA", "vote", "win", market="us_stock", void=True) == 1
    settled = {r["prediction_value"]: r["is_correct"]
               for r in store.list_settled_predictions(prediction_types=["vote"], market="us_stock")}
    assert settled == {"approve": 1, "reject": 0}  # 作废(-2)不入校准


# —— VIP 关注对称判 ——

def test_watch_advice_symmetric():
    assert _judge("关注", 1.0) is True
    assert _judge("关注", 30.0) is False  # 大涨=踏空
    assert _judge("关注", -30.0) is False
    assert _judge("持有", 30.0) is True
    assert _judge("未知动作", 5.0) is None


# —— 配额闩锁 ——

def test_quota_latch():
    dps._reset_for_test()
    assert dps.is_quota_message("抱歉，您每天最多访问该接口 10 次")
    assert not dps.is_quota_message("ok")
    assert not dps.is_over_quota("tushare_test_src")
    dps.latch_until_tomorrow("tushare_test_src")
    assert dps.is_over_quota("tushare_test_src")
    dps._reset_for_test()
    assert not dps.is_over_quota("tushare_test_src")


# —— P0-2：PBO / DSR / 块自助 / 过拟合标记 / 成本压测 ——

def test_pbo_pure_noise_vs_true_signal():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, size=(320, 20))
    assert 0.2 < pbo_cscv(noise, n_splits=8) < 0.8  # 纯噪声：选最优≈抛硬币
    good = noise.copy()
    good[:, 0] += 0.01  # 一个真有 alpha 的配置
    assert pbo_cscv(good, n_splits=8) < 0.1


def test_dsr_penalizes_many_trials():
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.01, 500)
    few = deflated_sharpe(r, [0.1, 0.0])
    many = deflated_sharpe(r, list(rng.normal(0, 0.05, 200)))
    assert 0.0 <= many < few <= 1.0


def test_overfit_flags():
    assert overfit_flags(pbo=0.6)["suspected_overfit"]
    assert overfit_flags(dsr=0.5)["suspected_overfit"]
    ok = overfit_flags(pbo=0.2, dsr=0.95)
    assert not ok["suspected_overfit"]


def test_block_bootstrap_ci_contains_mean():
    rng = np.random.default_rng(2)
    x = rng.normal(1.0, 1.0, 300)
    ci = block_bootstrap_ci(x, n_resamples=500, seed=0)
    assert ci.low < 1.0 < ci.high


def test_cost_stress_monotone():
    days = [f"2026-01-{d:02d}" for d in range(2, 22)]
    bars = {"AAA": [Bar(d, 100.0 + i, volume=1e6) for i, d in enumerate(days)]}
    orders = [Order(days[0], "AAA", "buy", 1000), Order(days[10], "AAA", "sell", 1000),
              Order(days[11], "AAA", "buy", 1000), Order(days[15], "AAA", "sell", 1000)]
    res = cost_stress(bars, orders)
    rets = [res[m].total_return_pct for m in (1.0, 1.5, 2.0, 3.0)]
    assert rets == sorted(rets, reverse=True) and rets[0] > rets[-1]


# —— P0-3：证据 ID ——

def test_evidence_index_check():
    idx = EvidenceIndex()
    a = idx.add("NVDA", "close", "2026-10-09", 180.5)
    assert a == evidence_id("NVDA", "close", "2026-10-09", 180.5)
    out = idx.check({"evidence_ids": [a, "fabricated01"]})
    assert out["grounded"] and out["evidence_ids"] == [a] and out["evidence_invalid_ids"] == ["fabricated01"]
    bad = idx.check({"vote": "approve"})
    assert bad["grounded"] is False and bad["evidence_label"] == UNGROUNDED_LABEL
    assert "grounded" not in EvidenceIndex().check({"vote": "approve"})  # 无证据可引时不判


def test_ungrounded_approve_not_counted():
    reviews = {
        "risk_officer": {"vote": "approve", "grounded": True},
        "growth_investor": {"vote": "approve", "grounded": False},
        "value_investor": {"vote": "approve", "grounded": False},
        "contrarian": {"vote": "reject", "grounded": False},  # 反对票不设证据门槛
    }
    c = _fallback_consensus(reviews)
    assert c["vote_detail"]["growth_investor"]["evidence_label"] == UNGROUNDED_LABEL
    assert c["final_verdict"] != "approved"  # 有效仅 1 赞 1 反
    assert UNGROUNDED_LABEL in c["summary"]
    # 未带 grounded 字段（旧数据/无证据索引）保持原计票
    legacy = {k: {"vote": "approve"} for k in reviews}
    assert _fallback_consensus(legacy)["final_verdict"] == "approved"
