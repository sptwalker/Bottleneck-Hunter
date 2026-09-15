"""P0-6 评估层测试：walk-forward 切分、bootstrap 区间、消融显著性、样本外汇总。"""

import numpy as np
import pytest

from bottleneck_hunter.watchlist.evaluation import (
    Split,
    ablation,
    bootstrap_ci,
    summarize_oos,
    walk_forward_splits,
)

DATES = [f"2026-01-{d:02d}" for d in range(1, 21)]  # 20 个连续日期


# —— walk-forward 切分：滚动、不重叠、防泄漏 ——

def test_walk_forward_basic_non_overlapping():
    splits = walk_forward_splits(DATES, train_size=5, test_size=2)
    assert all(isinstance(s, Split) for s in splits)
    first = splits[0]
    assert first.train == tuple(DATES[0:5])
    assert first.test == tuple(DATES[5:7])
    # 默认 step=test_size=2，第二窗前移 2
    assert splits[1].train == tuple(DATES[2:7])
    # 每个窗口 train 末日严格早于 test 首日（无泄漏）
    assert all(s.train[-1] < s.test[0] for s in splits)


def test_walk_forward_custom_step():
    splits = walk_forward_splits(DATES, train_size=5, test_size=2, step=5)
    assert splits[1].train == tuple(DATES[5:10])


def test_walk_forward_insufficient_data_returns_empty():
    assert walk_forward_splits(DATES[:6], train_size=5, test_size=2) == []


def test_walk_forward_dedups_and_sorts():
    messy = ["2026-01-03", "2026-01-01", "2026-01-01", "2026-01-02", "2026-01-04"]
    splits = walk_forward_splits(messy, train_size=2, test_size=1)
    assert splits[0].train == ("2026-01-01", "2026-01-02")
    assert splits[0].test == ("2026-01-03",)


def test_walk_forward_rejects_bad_args():
    with pytest.raises(ValueError):
        walk_forward_splits(DATES, train_size=0, test_size=2)
    with pytest.raises(ValueError):
        walk_forward_splits(DATES, train_size=5, test_size=2, step=0)


# —— bootstrap 区间：可复现、含点估计、边界 ——

def test_bootstrap_ci_point_is_mean():
    ci = bootstrap_ci([1.0, 2.0, 3.0, 4.0, 5.0])
    assert ci.point == pytest.approx(3.0)
    assert ci.low <= ci.point <= ci.high


def test_bootstrap_ci_reproducible_with_seed():
    data = [0.1, -0.2, 0.3, 0.05, -0.1, 0.2]
    a = bootstrap_ci(data, seed=42)
    b = bootstrap_ci(data, seed=42)
    assert (a.low, a.point, a.high) == (b.low, b.point, b.high)


def test_bootstrap_ci_different_seed_differs():
    data = [0.1, -0.2, 0.3, 0.05, -0.1, 0.2]
    a = bootstrap_ci(data, seed=1)
    b = bootstrap_ci(data, seed=2)
    assert (a.low, a.high) != (b.low, b.high)


def test_bootstrap_ci_single_sample_is_degenerate():
    ci = bootstrap_ci([2.5])
    assert ci.low == ci.point == ci.high == 2.5


def test_bootstrap_ci_rejects_empty_and_bad_confidence():
    with pytest.raises(ValueError):
        bootstrap_ci([])
    with pytest.raises(ValueError):
        bootstrap_ci([1.0, 2.0], confidence=1.5)


# —— 消融：显著性判定、方向、可复现 ——

def test_ablation_detects_clear_positive_increment():
    baseline = [0.0, 0.01, -0.01, 0.0, 0.005, -0.005] * 5
    variant = [x + 0.05 for x in baseline]  # 一致抬升 → 配对差恒正
    result = ablation(baseline, variant, seed=0)
    assert result.significant
    assert result.direction == "positive"
    assert result.delta == pytest.approx(0.05, abs=1e-9)
    assert result.ci.low > 0


def test_ablation_inconclusive_when_no_net_effect():
    # 变体在基线上加对称零和扰动：净差恒为 0 → 不应判显著（避免用随机抽样，其有限样本本就可能偶然显著）
    baseline = [0.1, -0.2, 0.3, 0.05, -0.1, 0.2, 0.0, -0.05] * 5
    variant = [x + (0.02 if i % 2 == 0 else -0.02) for i, x in enumerate(baseline)]
    result = ablation(baseline, variant, seed=0)
    assert result.delta == pytest.approx(0.0, abs=1e-9)
    assert not result.significant
    assert result.direction == "inconclusive"


def test_ablation_reports_negative_direction():
    baseline = [0.1, 0.12, 0.09, 0.11, 0.1, 0.1] * 4
    variant = [x - 0.05 for x in baseline]
    result = ablation(baseline, variant, seed=0)
    assert result.significant
    assert result.direction == "negative"


def test_ablation_handles_unequal_lengths():
    baseline = [0.0, 0.01, -0.01, 0.0] * 6
    variant = [0.05, 0.06, 0.04] * 8  # 不同长度 → 走独立重采样分支
    result = ablation(baseline, variant, seed=0)
    assert result.direction == "positive"
    assert result.delta > 0


def test_ablation_reproducible():
    baseline = [0.0, 0.01, -0.01, 0.02, -0.02, 0.0]
    variant = [0.03, 0.02, 0.01, 0.04, 0.0, 0.02]
    a = ablation(baseline, variant, seed=5)
    b = ablation(baseline, variant, seed=5)
    assert (a.delta, a.ci.low, a.ci.high, a.significant) == (b.delta, b.ci.low, b.ci.high, b.significant)


# —— 样本外汇总 ——

def test_summarize_oos_standard_indicators():
    returns = [0.01, -0.005, 0.02, 0.0, 0.015, -0.01]
    summary = summarize_oos(returns, seed=0)
    assert summary["n_periods"] == 6
    assert summary["mean_return"] == pytest.approx(np.mean(returns))
    assert summary["volatility"] == pytest.approx(np.std(returns, ddof=1))
    assert summary["mean_ci_low"] <= summary["mean_return"] <= summary["mean_ci_high"]


def test_summarize_oos_rejects_empty():
    with pytest.raises(ValueError):
        summarize_oos([])
