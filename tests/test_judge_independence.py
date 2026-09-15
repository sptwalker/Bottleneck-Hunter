"""P1-2 评委有效独立性测试：相似度矩阵、有效独立评委数、冗余校正权重、加权计票。"""

import numpy as np
import pytest

from bottleneck_hunter.watchlist.judge_independence import (
    analyze_independence,
    effective_number_of_judges,
    independence_weights,
    vote_similarity_matrix,
    weighted_approval,
)

# 三条独立且方差非零的历史投票序列，供“全独立”场景复用。
_INDEP = {
    "risk_officer": ["approve", "reject", "approve", "reject", "abstain"],
    "growth_investor": ["reject", "reject", "approve", "approve", "reject"],
    "value_investor": ["approve", "approve", "reject", "reject", "approve"],
}


# —— 相似度矩阵 ——

def test_similarity_identical_judges_is_one():
    votes = {
        "a": ["approve", "reject", "approve", "reject"],
        "b": ["approve", "reject", "approve", "reject"],
    }
    _, sim = vote_similarity_matrix(votes)
    assert sim[0, 1] == pytest.approx(1.0)
    assert sim[1, 0] == pytest.approx(1.0)
    assert np.allclose(np.diag(sim), 1.0)


def test_similarity_negative_correlation_clamped_to_zero():
    # 完全反向投票：Pearson=-1，钳到 0（真分歧视作独立，不反向增益）。
    votes = {
        "a": ["approve", "reject", "approve", "reject"],
        "b": ["reject", "approve", "reject", "approve"],
    }
    _, sim = vote_similarity_matrix(votes)
    assert sim[0, 1] == 0.0


def test_similarity_constant_votes_use_agreement_rate():
    # 零方差 Pearson 未定义：恒定且相同→1。
    votes = {
        "a": ["approve", "approve", "approve"],
        "b": ["approve", "approve", "approve"],
    }
    _, sim = vote_similarity_matrix(votes)
    assert sim[0, 1] == pytest.approx(1.0)


def test_similarity_constant_but_different_partial_agreement():
    votes = {
        "a": ["approve", "approve", "approve", "approve"],
        "b": ["approve", "approve", "reject", "reject"],
    }
    _, sim = vote_similarity_matrix(votes)
    assert sim[0, 1] == pytest.approx(0.5)


def test_similarity_matrix_symmetric():
    _, sim = vote_similarity_matrix(_INDEP)
    assert np.allclose(sim, sim.T)


# —— 有效独立评委数 ——

def test_neff_identical_judges_collapse_to_one():
    votes = {r: ["approve", "reject", "approve", "reject"] for r in ("a", "b", "c")}
    _, sim = vote_similarity_matrix(votes)
    assert effective_number_of_judges(sim) == pytest.approx(1.0)


def test_neff_fully_independent_equals_n():
    _, sim = vote_similarity_matrix(_INDEP)
    # 三条互不相关序列相似度≈0（对角为1），N_eff≈N。
    assert effective_number_of_judges(sim) == pytest.approx(3.0, abs=0.3)


def test_neff_between_one_and_n():
    _, sim = vote_similarity_matrix(_INDEP)
    n = sim.shape[0]
    neff = effective_number_of_judges(sim)
    assert 1.0 <= neff <= n + 1e-9


# —— 冗余校正权重：核心验收「权重不以人数简单相加」 ——

def test_duplicate_judges_do_not_linearly_stack():
    # 3 个完全相同评委：朴素加权=3，独立性校正后合计≈1。
    votes = {r: ["approve", "reject", "approve", "reject"] for r in ("a", "b", "c")}
    _, sim = vote_similarity_matrix(votes)
    eff = independence_weights(sim)
    assert eff.sum() == pytest.approx(1.0)
    assert np.allclose(eff, 1.0 / 3.0)


def test_independent_judges_keep_full_weight():
    _, sim = vote_similarity_matrix(_INDEP)
    eff = independence_weights(sim)
    # 近独立：每人权重≈1，几乎不被稀释。
    assert np.all(eff > 0.85)
    assert eff.sum() == pytest.approx(3.0, abs=0.4)


def test_base_weights_respected():
    votes = {r: ["approve", "reject", "approve", "reject"] for r in ("a", "b")}
    _, sim = vote_similarity_matrix(votes)  # 两个相同评委，各自冗余=2
    eff = independence_weights(sim, base_weights=[2.0, 4.0])
    assert eff[0] == pytest.approx(1.0)  # 2/2
    assert eff[1] == pytest.approx(2.0)  # 4/2


def test_independence_weights_rejects_negative_base():
    sim = np.eye(2)
    with pytest.raises(ValueError):
        independence_weights(sim, base_weights=[-1.0, 1.0])


def test_independence_weights_rejects_shape_mismatch():
    sim = np.eye(3)
    with pytest.raises(ValueError):
        independence_weights(sim, base_weights=[1.0, 1.0])


# —— 一站式报告 ——

def test_analyze_independence_report_fields():
    rep = analyze_independence(_INDEP)
    assert rep.roles == tuple(_INDEP)
    assert rep.n_members == 3
    assert 1.0 <= rep.n_effective <= 3.0 + 1e-9
    assert len(rep.effective_weights) == 3
    assert len(rep.redundancy) == 3


def test_analyze_independence_duplicate_collapses():
    votes = {r: ["approve", "reject", "approve"] for r in ("a", "b", "c")}
    rep = analyze_independence(votes)
    assert rep.n_effective == pytest.approx(1.0)
    assert sum(rep.effective_weights) == pytest.approx(1.0)


# —— 加权计票 ——

def test_weighted_approval_naive_vs_effective():
    # 3 相同赞成 + 1 反对：朴素 3:1 → 0.75；有效独立后赞成侧被折叠。
    votes = {"a": "approve", "b": "approve", "c": "approve", "d": "reject"}
    naive = weighted_approval(votes)
    assert naive["approve_ratio"] == pytest.approx(0.75)

    eff_weights = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3, "d": 1.0}
    eff = weighted_approval(votes, eff_weights)
    assert eff["approve_ratio"] == pytest.approx(0.5)  # 1:1


def test_weighted_approval_abstain_excluded_from_denominator():
    votes = {"a": "approve", "b": "reject", "c": "abstain"}
    res = weighted_approval(votes)
    assert res["w_approve"] == pytest.approx(1.0)
    assert res["w_reject"] == pytest.approx(1.0)
    assert res["approve_ratio"] == pytest.approx(0.5)


def test_weighted_approval_all_abstain_zero_ratio():
    res = weighted_approval({"a": "abstain", "b": "abstain"})
    assert res["approve_ratio"] == 0.0


def test_weighted_approval_rejects_negative_weight():
    with pytest.raises(ValueError):
        weighted_approval({"a": "approve"}, {"a": -1.0})


# —— 输入校验 ——

def test_empty_vote_vectors_rejected():
    with pytest.raises(ValueError):
        vote_similarity_matrix({})


def test_mismatched_lengths_rejected():
    with pytest.raises(ValueError):
        vote_similarity_matrix({"a": ["approve"], "b": ["approve", "reject"]})


def test_empty_series_rejected():
    with pytest.raises(ValueError):
        vote_similarity_matrix({"a": [], "b": []})


def test_effective_number_rejects_non_square():
    with pytest.raises(ValueError):
        effective_number_of_judges(np.ones((2, 3)))
