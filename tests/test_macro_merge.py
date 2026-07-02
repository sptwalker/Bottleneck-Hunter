"""L1 宏观策略双模型合并逻辑自检 — _merge_macro_results 真交叉验证。"""

from bottleneck_hunter.watchlist.decision_engine import (
    _as_num,
    _merge_key_signals,
    _merge_macro_results,
    _merge_sector_rotation,
    _union_strs,
)


def test_single_result_passthrough():
    r = {"regime": "bull", "risk_appetite": "aggressive", "regime_confidence": 8}
    assert _merge_macro_results([r]) is r  # 单模型原样返回，不动


def test_majority_vote_and_agreement():
    a = {"regime": "bull", "risk_appetite": "balanced", "regime_confidence": 8}
    b = {"regime": "bull", "risk_appetite": "balanced", "regime_confidence": 6}
    m = _merge_macro_results([a, b])
    assert m["regime"] == "bull"
    assert m["risk_appetite"] == "balanced"
    assert m["regime_confidence"] == 7.0  # 均值，无分歧不惩罚
    assert "_divergence_warning" not in m


def test_regime_divergence_warns_and_penalizes():
    a = {"regime": "bull", "risk_appetite": "balanced", "regime_confidence": 8}
    b = {"regime": "bear", "risk_appetite": "balanced", "regime_confidence": 8}
    m = _merge_macro_results([a, b])
    # 平票时取第一个；关键是分歧被标记且 confidence 被下调
    assert m["regime"] in ("bull", "bear")
    assert m["regime_confidence"] == 7.0  # 8 - 1(regime 分歧)
    assert "regime 不一致" in m["_divergence_warning"]


def test_appetite_divergence_also_warns():
    a = {"regime": "bull", "risk_appetite": "aggressive", "regime_confidence": 6}
    b = {"regime": "bull", "risk_appetite": "defensive", "regime_confidence": 6}
    m = _merge_macro_results([a, b])
    assert m["regime_confidence"] == 5.0  # 6 - 1(appetite 分歧)
    assert "risk_appetite 不一致" in m["_divergence_warning"]


def test_risk_factors_union_not_dropped():
    a = {"regime": "bull", "risk_factors": ["利率上行", "估值高"]}
    b = {"regime": "bull", "risk_factors": ["地缘冲突", "估值高"]}
    m = _merge_macro_results([a, b])
    # 两个模型的风险都保留，去重
    assert set(m["risk_factors"]) == {"利率上行", "估值高", "地缘冲突"}


def test_strategy_text_from_majority_regime_model():
    # 少数派在前，多数派 regime 应决定 strategy_text 主体
    minority = {"regime": "bear", "strategy_text": "熊市论述", "regime_confidence": 5}
    maj1 = {"regime": "bull", "strategy_text": "牛市论述A", "regime_confidence": 7}
    maj2 = {"regime": "bull", "strategy_text": "牛市论述B", "regime_confidence": 7}
    m = _merge_macro_results([minority, maj1, maj2])
    assert m["regime"] == "bull"
    assert m["strategy_text"] == "牛市论述A"  # 命中多数 regime 的首个模型，而非 results[0]


def test_recommended_cash_pct_averaged():
    a = {"regime": "bull", "recommended_cash_pct": 20}
    b = {"regime": "bull", "recommended_cash_pct": 40}
    m = _merge_macro_results([a, b])
    assert m["recommended_cash_pct"] == 30.0


def test_confidence_floor_is_one():
    a = {"regime": "bull", "risk_appetite": "aggressive", "regime_confidence": 1}
    b = {"regime": "bear", "risk_appetite": "defensive", "regime_confidence": 1}
    m = _merge_macro_results([a, b])
    assert m["regime_confidence"] == 1  # 1 - 2 惩罚后仍钳到下限 1


def test_as_num_coercion():
    assert _as_num("25%", 0) == 25.0
    assert _as_num(7, 0) == 7.0
    assert _as_num("bad", 5) == 5
    assert _as_num(None, 5) == 5
    assert _as_num(True, 5) == 5  # bool 不当数字


def test_sector_rotation_merge_removes_conflicting_neutral():
    a = {"strengthening": ["半导体"], "weakening": [], "neutral": ["银行"]}
    b = {"strengthening": [], "weakening": ["银行"], "neutral": ["半导体"]}
    merged = _merge_sector_rotation([a, b])
    assert "半导体" in merged["strengthening"]
    assert "银行" in merged["weakening"]
    # 银行/半导体已在强弱桶，不应残留 neutral（自相矛盾）
    assert merged["neutral"] == []


def test_key_signals_dedup_by_name():
    a = [{"name": "VIX", "value": "18"}, {"name": "利率", "value": "4.2%"}]
    b = [{"name": "vix", "value": "19"}]  # 同名（大小写）应去重
    merged = _merge_key_signals([a, b])
    names = [s["name"].lower() for s in merged]
    assert names.count("vix") == 1
    assert len(merged) == 2


def test_union_strs_ignores_non_list():
    assert _union_strs([["a"], None, "notalist", ["a", "b"]]) == ["a", "b"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
