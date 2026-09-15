"""P1-2 评委分组、相关性与有效独立性。

投委会加权表决默认按「历史权重 × 票数」线性相加（`committee._fallback_consensus`），
但当多位评委高度相关（同源模型、历史投票同步、甚至重复评委）时，线性相加等于把同一份
信号重复计数，高估共识强度。本模块提供**有效独立性**校正：

- `vote_similarity_matrix`：由历史投票向量算两两相似度（Pearson 钳到 [0,1]；零方差退化为逐元素一致率）。
- `effective_number_of_judges`：N_eff = N² / Σ相似度；k 个完全相同评委 → N_eff=1，全独立 → N_eff=N。
- `independence_weights`：每位评委权重除以其冗余簇规模（相似度行和），使相关评委不再线性叠加——
  k 个相同评委合计有效权重 = 单个评委权重，而非 k 倍。
- `weighted_approval`：给定票与权重的加权赞成/反对质量与赞成率，便于对比朴素计票 vs 有效独立计票。

纯 numpy 确定可复现；不引入 scipy。与 `committee.py` 分层：本模块只做度量与权重校正，
`_fallback_consensus` 的既有等权/加权表决保持不变（回退旧聚合器只需不调用本模块）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

# 与 committee._VALID_VOTES 对齐：赞成族 +1、反对 -1、弃权/未知 0。
_APPROVE = {"approve", "approve_with_modification"}
_REJECT = {"reject"}


def _vote_to_score(vote: str) -> float:
    if vote in _APPROVE:
        return 1.0
    if vote in _REJECT:
        return -1.0
    return 0.0


def _pair_similarity(a: np.ndarray, b: np.ndarray) -> float:
    # 负相关=真分歧，钳到 0 视作独立，不做反向增益。
    if a.std() < 1e-12 or b.std() < 1e-12:
        # 零方差（恒定投票）Pearson 未定义：退化为逐元素一致率，恒定且相同→1，否则按一致比例。
        return min(1.0, max(0.0, float(np.mean(a == b))))
    return min(1.0, max(0.0, float(np.corrcoef(a, b)[0, 1])))


def vote_similarity_matrix(vote_vectors: dict[str, Sequence]) -> tuple[tuple[str, ...], np.ndarray]:
    """由各评委历史投票向量算两两相似度矩阵（对称、对角 1、值域 [0,1]）。"""
    if not vote_vectors:
        raise ValueError("需要至少一位评委的历史投票")
    roles = tuple(vote_vectors)
    series = [np.array([_vote_to_score(v) for v in vote_vectors[r]], dtype=float) for r in roles]
    n_obs = series[0].size
    if n_obs == 0:
        raise ValueError("历史投票向量不能为空")
    if any(s.size != n_obs for s in series):
        raise ValueError("所有评委的历史投票长度必须一致")

    n = len(roles)
    sim = np.eye(n, dtype=float)
    for i in range(n):
        for j in range(i + 1, n):
            sim[i, j] = sim[j, i] = _pair_similarity(series[i], series[j])
    return roles, sim


def effective_number_of_judges(similarity: np.ndarray) -> float:
    """有效独立评委数 N_eff = N² / Σ相似度。全相同→1，全独立→N。"""
    s = np.asarray(similarity, dtype=float)
    if s.ndim != 2 or s.shape[0] != s.shape[1]:
        raise ValueError("相似度矩阵必须为方阵")
    total = float(np.clip(s, 0.0, 1.0).sum())
    return float(s.shape[0] ** 2 / total) if total > 0.0 else 0.0


def independence_weights(similarity: np.ndarray, base_weights: Sequence[float] | None = None) -> np.ndarray:
    """有效独立性校正权重：base_i / 冗余簇规模_i（相似度行和）。相关评委不再线性叠加。"""
    s = np.clip(np.asarray(similarity, dtype=float), 0.0, 1.0)
    if s.ndim != 2 or s.shape[0] != s.shape[1]:
        raise ValueError("相似度矩阵必须为方阵")
    n = s.shape[0]
    if base_weights is None:
        base = np.ones(n, dtype=float)
    else:
        base = np.asarray(base_weights, dtype=float)
        if base.shape != (n,):
            raise ValueError("base_weights 长度必须与评委数一致")
        if np.any(base < 0):
            raise ValueError("base_weights 不能为负")
    redundancy = np.maximum(s.sum(axis=1), 1.0)  # 对角=1 已保证 ≥1，兜底数值误差
    return base / redundancy


@dataclass(frozen=True)
class IndependenceReport:
    roles: tuple[str, ...]
    n_members: int
    n_effective: float                    # 有效独立评委数
    base_weights: tuple[float, ...]
    effective_weights: tuple[float, ...]  # 有效独立性校正后的权重
    redundancy: tuple[float, ...]         # 每位评委的冗余簇规模（相似度行和）


def analyze_independence(
    vote_vectors: dict[str, Sequence], base_weights: dict[str, float] | None = None
) -> IndependenceReport:
    """一站式：历史投票 → 相似度 → 有效独立评委数 + 有效权重。"""
    roles, sim = vote_similarity_matrix(vote_vectors)
    base = None if base_weights is None else [float(base_weights.get(r, 1.0)) for r in roles]
    eff = independence_weights(sim, base)
    base_arr = np.ones(len(roles)) if base is None else np.asarray(base, dtype=float)
    return IndependenceReport(
        roles=roles,
        n_members=len(roles),
        n_effective=effective_number_of_judges(sim),
        base_weights=tuple(float(x) for x in base_arr),
        effective_weights=tuple(float(x) for x in eff),
        redundancy=tuple(float(x) for x in np.clip(sim, 0.0, 1.0).sum(axis=1)),
    )


def weighted_approval(votes: dict[str, str], weights: dict[str, float] | None = None) -> dict:
    """加权赞成/反对质量与赞成率（与 committee 口径一致：赞成族 vs 反对，弃权不计入分母）。"""
    if not votes:
        raise ValueError("需要至少一票")
    w_approve = w_reject = 0.0
    for role, vote in votes.items():
        w = 1.0 if weights is None else float(weights.get(role, 1.0))
        if w < 0:
            raise ValueError("权重不能为负")
        if vote in _APPROVE:
            w_approve += w
        elif vote in _REJECT:
            w_reject += w
    decisive = w_approve + w_reject
    return {
        "w_approve": round(w_approve, 6),
        "w_reject": round(w_reject, 6),
        "approve_ratio": round((w_approve / decisive) if decisive > 0 else 0.0, 6),
    }
