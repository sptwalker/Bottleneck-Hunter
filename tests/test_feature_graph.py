"""P1-3 特征依赖图与重复计权检测测试：环检测、缺失依赖、重复特征、来源回溯、重复计权。"""

import pytest

from bottleneck_hunter.watchlist.feature_graph import (
    analyze_features,
    detect_duplicate_weighting,
    feature,
    resolve_sources,
    validate_graph,
)

# 一张健康的小型特征图：两个根来源经中间特征汇入两个计权特征，无环、无缺失、无重复来源。
_HEALTHY = [
    feature("raw_volume", sources=["volume"]),
    feature("raw_price", sources=["price"]),
    feature("vol_momentum", depends_on=["raw_volume"], weight=0.5),
    feature("price_trend", depends_on=["raw_price"], weight=0.5),
]


# —— 环检测 ——

def test_cycle_detected():
    specs = [
        feature("a", depends_on=["b"]),
        feature("b", depends_on=["a"]),
    ]
    report = validate_graph(specs)
    assert report.cycle  # 非空即检出环
    assert set(report.cycle) >= {"a", "b"}
    assert not report.ok
    assert report.order == ()  # 有环时不产出拓扑序


def test_self_loop_is_cycle():
    report = validate_graph([feature("a", depends_on=["a"])])
    assert report.cycle
    assert not report.ok


def test_acyclic_has_topological_order():
    report = validate_graph(_HEALTHY)
    assert report.cycle == ()
    assert report.ok
    order = report.order
    # 上游必须排在下游之前
    assert order.index("raw_volume") < order.index("vol_momentum")
    assert order.index("raw_price") < order.index("price_trend")


# —— 缺失依赖 ——

def test_missing_dependency_reported():
    specs = [feature("a", depends_on=["ghost"])]
    report = validate_graph(specs)
    assert report.missing == {"a": ("ghost",)}
    assert not report.ok


def test_sources_are_not_missing_deps():
    # sources 是外部叶子输入，不应被当作缺失特征
    report = validate_graph([feature("a", sources=["some_raw_input"], weight=1.0)])
    assert report.missing == {}
    assert report.ok


# —— 重复特征（来源集合相同）——

def test_duplicate_features_by_identical_sources():
    specs = [
        feature("vol_a", sources=["volume"]),
        feature("vol_b", sources=["volume"]),
        feature("price", sources=["price"]),
    ]
    report = validate_graph(specs)
    assert ("vol_a", "vol_b") in report.duplicate_features
    # 来源不同的不算重复
    assert all("price" not in g for g in report.duplicate_features)


def test_duplicate_feature_name_rejected():
    with pytest.raises(ValueError, match="重复特征名"):
        validate_graph([feature("a", sources=["x"]), feature("a", sources=["y"])])


# —— 来源回溯 ——

def test_resolve_sources_follows_dependency_chain():
    specs = [
        feature("raw", sources=["volume"]),
        feature("mid", depends_on=["raw"]),
        feature("top", depends_on=["mid"], sources=["extra"], weight=1.0),
    ]
    assert resolve_sources(specs, "top") == frozenset({"volume", "extra"})
    assert resolve_sources(specs, "mid") == frozenset({"volume"})


def test_resolve_sources_unknown_raises():
    with pytest.raises(KeyError):
        resolve_sources(_HEALTHY, "nonexistent")


def test_resolve_sources_diamond_no_double():
    # 钻石依赖：两条路径都回到同一根来源，集合天然去重
    specs = [
        feature("root", sources=["s"]),
        feature("left", depends_on=["root"]),
        feature("right", depends_on=["root"]),
        feature("sink", depends_on=["left", "right"], weight=1.0),
    ]
    assert resolve_sources(specs, "sink") == frozenset({"s"})


# —— 重复计权 ——

def test_detect_duplicate_weighting_shared_root():
    # 两个计权特征共享同一根来源 volume → 重复计权
    specs = [
        feature("raw_volume", sources=["volume"]),
        feature("vol_ratio", depends_on=["raw_volume"], weight=0.3),
        feature("vol_days", depends_on=["raw_volume"], weight=0.2),
    ]
    overlaps = detect_duplicate_weighting(specs)
    assert len(overlaps) == 1
    o = overlaps[0]
    assert {o.a, o.b} == {"vol_ratio", "vol_days"}
    assert o.shared_sources == ("volume",)


def test_no_duplicate_weighting_when_orthogonal():
    # 健康图的两个计权特征来源不相交 → 无重复计权（验证 FinalScorer「正交」断言的场景）
    assert detect_duplicate_weighting(_HEALTHY) == ()


def test_zero_weight_features_excluded_from_overlap():
    # 中间特征 weight=0 不参与计权，即便共享来源也不报
    specs = [
        feature("raw", sources=["x"]),
        feature("mid1", depends_on=["raw"]),  # weight=0
        feature("mid2", depends_on=["raw"]),  # weight=0
        feature("scored", depends_on=["mid1"], weight=1.0),
    ]
    assert detect_duplicate_weighting(specs) == ()


# —— 一站式 ——

def test_analyze_features_healthy():
    audit = analyze_features(_HEALTHY)
    assert audit.graph.ok
    assert audit.duplicate_weighting == ()


def test_analyze_features_flags_both_problems():
    specs = [
        feature("raw", sources=["v"]),
        feature("f1", depends_on=["raw"], weight=0.5),
        feature("f2", depends_on=["raw"], weight=0.5),
    ]
    audit = analyze_features(specs)
    assert audit.graph.ok  # 无环无缺失
    assert len(audit.duplicate_weighting) == 1  # 但有重复计权
