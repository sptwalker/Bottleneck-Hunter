"""P1-3 特征定义、依赖图与重复计权检测。

评分系统（`chain/supplier_eval.py` 的 `AlphaScorer`/`FinalScorer`）把多个特征加权汇总。
若两个特征其实源自同一原始信号（如 `volume_ratio` 与 `consecutive_volume_days` 都来自成交量），
却各自独立计权，等于把同一份信息重复计数——与 [[quant-research-upgrade-progress]] 里 P1-2
评委相关性同构，只是换到「特征」这条轴。`FinalScorer` 注释断言「两者完全正交，无维度重叠」，
本模块把该断言变成**可校验**的：

- `FeatureSpec`：每个特征声明 `name`、`sources`（原始来源键集合）、`depends_on`（上游特征名）、`weight`。
- `validate_graph`：环检测（stdlib graphlib）、缺失依赖、重复特征（来源集合完全相同的不同名特征）。
- `detect_duplicate_weighting`：各自带非零权重且共享根来源的特征对 → 重复计权告警。
- `resolve_sources`：沿依赖图回溯每个特征的全部根来源，使「特征可追溯来源与依赖」可验证。

纯 stdlib（graphlib/dataclasses/collections）确定可复现，不引入 numpy/scipy。
纯诊断层：不改评分口径、不接线进 `supplier_eval`，禁用检测只是不调用本模块（回退方式）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    sources: frozenset[str] = frozenset()      # 直接引用的原始来源键（叶子输入）
    depends_on: frozenset[str] = frozenset()   # 上游特征名（图的边）
    weight: float = 0.0                        # 加权汇总中的权重；0 = 不直接计权（中间特征）


def feature(name: str, sources: Iterable[str] = (), depends_on: Iterable[str] = (), weight: float = 0.0) -> FeatureSpec:
    """便捷构造：把任意可迭代来源/依赖归一为 frozenset。"""
    return FeatureSpec(name, frozenset(sources), frozenset(depends_on), float(weight))


def _index(specs: Iterable[FeatureSpec]) -> dict[str, FeatureSpec]:
    by_name: dict[str, FeatureSpec] = {}
    for s in specs:
        if s.name in by_name:
            raise ValueError(f"重复特征名: {s.name}")  # 同名会互相覆盖，直接拒绝而非静默丢弃
        by_name[s.name] = s
    return by_name


def _missing_deps(by_name: dict[str, FeatureSpec]) -> dict[str, tuple[str, ...]]:
    """每个特征 depends_on 中未定义的被依赖项（sources 是外部叶子，不算缺失）。"""
    return {
        name: tuple(sorted(d for d in spec.depends_on if d not in by_name))
        for name, spec in by_name.items()
        if any(d not in by_name for d in spec.depends_on)
    }


def _build_sorter(by_name: dict[str, FeatureSpec]) -> TopologicalSorter:
    ts: TopologicalSorter = TopologicalSorter()
    for name, spec in by_name.items():
        # 只连已定义的上游：缺失依赖交给 _missing_deps 单独报，避免被 graphlib 当作根节点掩盖
        ts.add(name, *(d for d in spec.depends_on if d in by_name))
    return ts


def _find_cycle(by_name: dict[str, FeatureSpec]) -> tuple[str, ...]:
    try:
        _build_sorter(by_name).prepare()
    except CycleError as e:
        return tuple(e.args[1])  # ponytail: graphlib 只报一条代表性环，多环时修一条重跑暴露下一条，诊断足够
    return ()


def _duplicate_features(by_name: dict[str, FeatureSpec]) -> tuple[tuple[str, ...], ...]:
    """来源集合完全相同的不同名特征分组（同一信号换名重复定义）。"""
    groups: dict[frozenset[str], list[str]] = defaultdict(list)
    for name, spec in by_name.items():
        if spec.sources:
            groups[spec.sources].append(name)
    return tuple(tuple(sorted(g)) for g in groups.values() if len(g) > 1)


@dataclass(frozen=True)
class GraphReport:
    order: tuple[str, ...]                            # 拓扑序（无环时；有环为空）
    cycle: tuple[str, ...]                            # 检出的一条环链（首尾同节点），无环为空
    missing: dict[str, tuple[str, ...]]               # feature -> 未定义的被依赖项
    duplicate_features: tuple[tuple[str, ...], ...]   # 来源相同的特征组
    ok: bool                                          # 无环且无缺失依赖


def validate_graph(specs: Iterable[FeatureSpec]) -> GraphReport:
    """一次性校验：环 + 缺失依赖 + 重复特征。"""
    by_name = _index(specs)
    cycle = _find_cycle(by_name)
    missing = _missing_deps(by_name)
    order = () if cycle else tuple(_build_sorter(by_name).static_order())
    return GraphReport(
        order=order,
        cycle=cycle,
        missing=missing,
        duplicate_features=_duplicate_features(by_name),
        ok=not cycle and not missing,
    )


def resolve_sources(specs: Iterable[FeatureSpec], name: str) -> frozenset[str]:
    """回溯一个特征的全部根来源（自身 sources ∪ 所有传递依赖的 sources）。"""
    by_name = _index(specs)
    if name not in by_name:
        raise KeyError(name)
    seen: set[str] = set()

    def _collect(n: str) -> set[str]:
        if n in seen:  # 环或钻石依赖：已访问不重复展开，天然防死循环
            return set()
        seen.add(n)
        spec = by_name.get(n)
        if spec is None:  # 缺失上游：无法贡献来源，交给 validate_graph 报缺失
            return set()
        roots = set(spec.sources)
        for d in spec.depends_on:
            roots |= _collect(d)
        return roots

    return frozenset(_collect(name))


@dataclass(frozen=True)
class WeightOverlap:
    a: str
    b: str
    shared_sources: tuple[str, ...]


def detect_duplicate_weighting(specs: Iterable[FeatureSpec]) -> tuple[WeightOverlap, ...]:
    """各自带非零权重且共享根来源的特征对——线性加权时同一信号被重复计权。"""
    by_name = _index(specs)
    weighted = sorted(n for n, s in by_name.items() if s.weight != 0.0)
    roots = {n: resolve_sources(specs, n) for n in weighted}
    out: list[WeightOverlap] = []
    for i, a in enumerate(weighted):
        for b in weighted[i + 1:]:
            shared = roots[a] & roots[b]
            if shared:
                out.append(WeightOverlap(a, b, tuple(sorted(shared))))
    return tuple(out)


@dataclass(frozen=True)
class FeatureAudit:
    graph: GraphReport
    duplicate_weighting: tuple[WeightOverlap, ...]


def analyze_features(specs: Iterable[FeatureSpec]) -> FeatureAudit:
    """一站式：图校验 + 重复计权检测。"""
    specs = list(specs)
    return FeatureAudit(validate_graph(specs), detect_duplicate_weighting(specs))
