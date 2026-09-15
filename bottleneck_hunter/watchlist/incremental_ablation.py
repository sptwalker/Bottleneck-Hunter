"""P2-3 LLM / 多 Agent / 投委会增量消融（组件阶梯）。

P0-6 `evaluation.ablation` 已能做「基线 vs 单一变体」的样本外差值 + bootstrap 区间 + 跨 0 显著性判定；
P1-2 `judge_independence.effective_number_of_judges` 已能算有效独立评委数 N_eff。本模块把两者编排成
**组件阶梯消融**，直接回答 P2-3 验收「证明增量或明确无增量，不以 persona 数量代替独立性」：

- 阶梯：按顺序给出若干配置臂（如 无LLM → 单LLM → 多Glob → 投委会），每臂带**同一组样本外窗口**下实测的逐窗收益。
- 逐级增量：相邻两臂用 `evaluation.ablation` 算增量 delta + 置信区间 + 显著性 + 方向（严格样本外、区间跨 0 即不显著）。
- 重复实验：对每一步用多个 bootstrap 种子重复，只有「显著且跨种子稳定」才算证明；一次显著但不稳定 → inconclusive。
- 明确无增量：跨种子一致不显著且增量区间紧贴 0（窄区间）→ `no_increment`（真零，而非样本不足）；否则 `inconclusive`。
- **独立性守卫**：投委会/多 Agent 臂携带 `n_components`（原始人数）与 `n_effective`（N_eff）；
  当人数增加但 N_eff 冗余高时置
  `persona_inflation=True`，报表同时呈现 delta 与 N_eff，杜绝「加了 5 个评委所以更好」这类以人数冒充独立信号的结论。
- 成本可选：每臂可带 `cost_usd`（复用 P2-2 成本模型口径），逐步给出 `cost_delta`，让「增量是否值回 LLM 成本」一目了然。

纯计算叶子层，复用 `evaluation` 与 `judge_independence`，不引入 scipy；**不接线进生产决策链**（回退=不调用，
对齐验收「不改变线上默认决策」）。各臂逐窗样本外收益须由调用方在严格 walk-forward（`walk_forward_splits`）下实测传入，
本模块不代跑回测、不臆造收益。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass

from bottleneck_hunter.watchlist.evaluation import ablation
from bottleneck_hunter.watchlist.judge_independence import (
    effective_number_of_judges,
    vote_similarity_matrix,
)


@dataclass(frozen=True)
class Arm:
    """消融阶梯上的一个配置臂：某组件配置在同一组样本外窗口下的实测逐窗收益。"""

    name: str
    oos_returns: tuple[float, ...]
    n_components: int = 1              # 原始组件/评委人数（投委会=persona 数）
    n_effective: float | None = None  # 有效独立数 N_eff（多 Glob/投委会臂给出；None=非集成臂）
    cost_usd: float = 0.0             # 该配置单位样本的 LLM 成本（可选，复用 P2-2 口径）


@dataclass(frozen=True)
class IncrementStep:
    """阶梯上相邻两臂之间的增量结论。"""

    from_arm: str
    to_arm: str
    delta: float
    ci_low: float
    ci_high: float
    significant: bool
    direction: str            # positive | negative | inconclusive
    stable_fraction: float    # 跨种子与主结论一致的比例
    verdict: str              # proven_positive | proven_negative | no_increment | inconclusive
    persona_inflation: bool   # 人数增加但 N_eff 冗余高（增量不可归因于人数）
    n_components_added: int
    n_effective_added: float
    cost_delta: float
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def n_eff_from_votes(vote_vectors: dict[str, Sequence]) -> float:
    """由历史投票向量直接算 N_eff（复用 P1-2），便于用真实投票构造投委会臂。"""
    _, sim = vote_similarity_matrix(vote_vectors)
    return effective_number_of_judges(sim)


def incremental_ablation(
    arms: Sequence[Arm],
    *,
    confidence: float = 0.95,
    n_resamples: int = 2000,
    seeds: Sequence[int] = (0, 1, 2, 3, 4),
    stability_threshold: float = 0.8,
    null_width: float = 0.01,
    redundancy_threshold: float = 0.5,
) -> dict:
    """对有序配置阶梯逐级做增量消融，返回逐步结论 + 总体裁决。

    arms：至少 2 个臂，按阶梯顺序排列；所有臂的 `oos_returns` 必须等长（同一组样本外窗口）。
    null_width：判定「明确无增量」的增量区间宽度上限（收益单位，校准旋钮）。
    redundancy_threshold：`1 - N_eff/人数` 超过它即判 persona 冗余膨胀（校准旋钮）。
    """
    arms = list(arms)
    if len(arms) < 2:
        raise ValueError("阶梯消融至少需要 2 个配置臂")
    if not seeds:
        raise ValueError("至少需要一个 bootstrap 种子")
    n_obs = len(arms[0].oos_returns)
    if n_obs == 0:
        raise ValueError("每个臂的样本外收益不能为空")
    if any(len(a.oos_returns) != n_obs for a in arms):
        raise ValueError("所有臂的样本外收益必须等长（同一组窗口）")

    steps: list[IncrementStep] = []
    for prev, cur in zip(arms[:-1], arms[1:], strict=True):
        steps.append(_one_step(prev, cur, confidence=confidence, n_resamples=n_resamples,
                               seeds=list(seeds), stability_threshold=stability_threshold,
                               null_width=null_width, redundancy_threshold=redundancy_threshold))

    proven_pos = [s for s in steps if s.verdict == "proven_positive"]
    proven_neg = [s for s in steps if s.verdict == "proven_negative"]
    return {
        "ladder": tuple(a.name for a in arms),
        "steps": steps,
        "n_windows": n_obs,
        "n_proven_positive": len(proven_pos),
        "n_proven_negative": len(proven_neg),
        "any_persona_inflation": any(s.persona_inflation for s in steps),
        "total_cost_delta": round(sum(s.cost_delta for s in steps), 6),
    }


def _one_step(prev: Arm, cur: Arm, *, confidence: float, n_resamples: int, seeds: list[int],
              stability_threshold: float, null_width: float, redundancy_threshold: float) -> IncrementStep:
    results = [ablation(prev.oos_returns, cur.oos_returns, n_resamples=n_resamples,
                        confidence=confidence, seed=s) for s in seeds]
    point = results[0]  # seed=seeds[0] 为确定性主结论

    if point.significant:
        agree = sum(1 for r in results if r.significant and r.direction == point.direction) / len(results)
    else:
        agree = sum(1 for r in results if not r.significant) / len(results)
    stable = agree >= stability_threshold

    width = point.ci.high - point.ci.low
    if point.significant and stable:
        verdict = "proven_positive" if point.direction == "positive" else "proven_negative"
    elif (not point.significant) and stable and width <= null_width:
        verdict = "no_increment"
    else:
        verdict = "inconclusive"

    # 独立性守卫：人数增加但 N_eff 冗余高 → 增量不可归因于人数
    added = cur.n_components - prev.n_components
    prev_eff = prev.n_effective if prev.n_effective is not None else float(prev.n_components)
    cur_eff = cur.n_effective if cur.n_effective is not None else float(cur.n_components)
    eff_added = cur_eff - prev_eff
    persona_inflation = False
    note = ""
    if cur.n_effective is not None and cur.n_components > 0:
        redundancy = 1.0 - cur.n_effective / cur.n_components
        if added > 0 and redundancy >= redundancy_threshold:
            persona_inflation = True
            note = (f"人数+{added} 但有效独立仅+{eff_added:.2f}（N_eff={cur.n_effective:.2f}/{cur.n_components}，"
                    f"冗余{redundancy:.0%}）：增量须归因于有效独立评委而非人数堆叠")

    return IncrementStep(
        from_arm=prev.name, to_arm=cur.name,
        delta=round(point.delta, 6), ci_low=round(point.ci.low, 6), ci_high=round(point.ci.high, 6),
        significant=point.significant, direction=point.direction,
        stable_fraction=round(agree, 6), verdict=verdict,
        persona_inflation=persona_inflation,
        n_components_added=added, n_effective_added=round(eff_added, 6),
        cost_delta=round(cur.cost_usd - prev.cost_usd, 6), note=note,
    )


if __name__ == "__main__":
    # ponytail 自检：证明增量 / 明确无增量 / inconclusive / persona 膨胀守卫 / N_eff / 阶梯汇总
    b = (0.01, 0.02, 0.00, 0.03, 0.01, 0.02, 0.00, 0.01)
    better = tuple(round(x + 0.05, 6) for x in b)          # 配对恒正差 → 显著正、跨种子稳定
    same = b                                                # 零差 → 明确无增量
    noisy = tuple(round(x + (0.1 if i % 2 == 0 else -0.1), 6) for i, x in enumerate(b))  # 宽区间跨 0 → inconclusive

    # 证明正增量
    rep = incremental_ablation([Arm("base", b), Arm("llm", better)])
    s = rep["steps"][0]
    assert s.verdict == "proven_positive" and s.direction == "positive" and s.significant
    assert s.stable_fraction == 1.0 and s.delta > 0

    # 明确无增量（真零）
    rep0 = incremental_ablation([Arm("base", b), Arm("noop", same)])
    assert rep0["steps"][0].verdict == "no_increment", rep0["steps"][0].verdict
    assert rep0["steps"][0].significant is False

    # 证据不足（宽区间跨 0）
    repn = incremental_ablation([Arm("base", b), Arm("noisy", noisy)])
    assert repn["steps"][0].verdict == "inconclusive", repn["steps"][0].verdict

    # 独立性守卫：投委会臂人数 6 但 N_eff=1.5（高冗余）→ persona_inflation，且增量虽真也须归因 N_eff
    rep_c = incremental_ablation([
        Arm("single", b, n_components=1, n_effective=1.0),
        Arm("committee", better, n_components=6, n_effective=1.5),
    ])
    sc = rep_c["steps"][0]
    assert sc.verdict == "proven_positive" and sc.persona_inflation is True
    assert sc.n_components_added == 5 and "N_eff" in sc.note
    assert rep_c["any_persona_inflation"] is True

    # 独立性守卫反例：人数 3 且 N_eff=2.8（低冗余）→ 不算膨胀
    rep_ok = incremental_ablation([
        Arm("single", b, n_components=1, n_effective=1.0),
        Arm("diverse", better, n_components=3, n_effective=2.8),
    ])
    assert rep_ok["steps"][0].persona_inflation is False

    # N_eff 由投票直接算：两个完全相同评委 → N_eff≈1；一正一反 → N_eff≈2
    same_votes = {"a": ["approve", "reject", "approve"], "b": ["approve", "reject", "approve"]}
    opp_votes = {"a": ["approve", "approve", "approve"], "b": ["reject", "reject", "reject"]}
    assert abs(n_eff_from_votes(same_votes) - 1.0) < 1e-6
    assert abs(n_eff_from_votes(opp_votes) - 2.0) < 1e-6

    # 成本增量 + 多级阶梯
    rep_ladder = incremental_ablation([
        Arm("base", b, cost_usd=0.0),
        Arm("llm", better, cost_usd=0.002),
        Arm("committee", better, n_components=4, n_effective=1.2, cost_usd=0.01),
    ])
    assert rep_ladder["ladder"] == ("base", "llm", "committee")
    assert len(rep_ladder["steps"]) == 2
    assert rep_ladder["steps"][0].cost_delta == 0.002
    assert rep_ladder["total_cost_delta"] == 0.01
    # base→llm 证明正增量；llm→committee 零差且加人 → 无增量 + persona 膨胀（加人不加信号更该警惕）
    assert rep_ladder["steps"][0].verdict == "proven_positive"
    assert rep_ladder["steps"][1].verdict == "no_increment"
    assert rep_ladder["steps"][1].persona_inflation is True

    # 边界
    try:
        incremental_ablation([Arm("only", b)])
        raise AssertionError("单臂应报错")
    except ValueError:
        pass
    try:
        incremental_ablation([Arm("a", b), Arm("b", b[:4])])
        raise AssertionError("不等长应报错")
    except ValueError:
        pass

    # 确定性：同输入同种子必得同结论
    r1 = incremental_ablation([Arm("x", b), Arm("y", better)])
    r2 = incremental_ablation([Arm("x", b), Arm("y", better)])
    assert r1["steps"][0].as_dict() == r2["steps"][0].as_dict()

    print("incremental_ablation 自检通过：增量证明/无增量/inconclusive + persona 守卫 + N_eff + 成本 + 确定性")
