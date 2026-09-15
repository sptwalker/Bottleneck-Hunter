"""P1-1 概率化信号与校准测试：Brier/LogLoss/ECE/可靠性曲线 + 等序回归校准与隔离。"""

import math

import numpy as np
import pytest

from bottleneck_hunter.watchlist.signal_calibration import (
    IsotonicCalibrator,
    brier_score,
    calibrate_and_evaluate,
    expected_calibration_error,
    log_loss,
    reliability_curve,
)

# —— Brier：均方概率误差 ——

def test_brier_perfect_and_worst():
    assert brier_score([1, 1, 0, 0], [1, 1, 0, 0]) == 0.0
    assert brier_score([0, 0, 1, 1], [1, 1, 0, 0]) == 1.0


def test_brier_known_value():
    assert brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)


# —— LogLoss：对数损失 ——

def test_log_loss_known_value():
    assert log_loss([0.5, 0.5], [1, 0]) == pytest.approx(-math.log(0.5))


def test_log_loss_confident_wrong_worse_than_confident_right():
    assert log_loss([0.99, 0.99], [1, 1]) < log_loss([0.01, 0.01], [1, 1])


def test_log_loss_perfect_is_near_zero():
    # eps 钳制使确信正确不至于 -inf，结果趋近 0
    assert log_loss([1.0, 0.0], [1, 0]) == pytest.approx(0.0, abs=1e-12)


# —— 概率/结果合法性校验 ——

@pytest.mark.parametrize("probs,outcomes", [
    ([1.5], [1]),          # 概率 > 1
    ([-0.1], [0]),         # 概率 < 0
    ([0.5, 0.5], [1]),     # 形状不一致
    ([0.5], [2]),          # 结果非 0/1
    ([], []),              # 空样本
])
def test_validation_rejects_illegal_inputs(probs, outcomes):
    with pytest.raises(ValueError):
        brier_score(probs, outcomes)


# —— 可靠性曲线：分桶、空桶、末桶闭区间 ——

def test_reliability_curve_bins_and_empty():
    bins = reliability_curve([0.05, 0.15, 0.95], [0, 0, 1], n_bins=10)
    assert len(bins) == 10
    assert bins[0].count == 1 and bins[0].mean_pred == pytest.approx(0.05) and bins[0].empirical == 0.0
    assert bins[1].count == 1 and bins[1].mean_pred == pytest.approx(0.15)
    assert bins[9].count == 1 and bins[9].empirical == 1.0  # [0.9,1.0] 闭区间纳入 0.95
    assert bins[5].count == 0 and math.isnan(bins[5].mean_pred)  # 空桶为 nan


def test_reliability_curve_includes_prob_one_in_last_bin():
    bins = reliability_curve([1.0], [1], n_bins=4)
    assert bins[3].count == 1 and bins[3].mean_pred == 1.0


# —— ECE：期望校准误差 ——

def test_ece_perfect_calibration_is_zero():
    assert expected_calibration_error([1.0, 1.0, 1.0, 1.0], [1, 1, 1, 1]) == 0.0


def test_ece_overconfident_positive():
    # 全预测 1.0 但实际半数为负 → |0.5-1.0| = 0.5
    assert expected_calibration_error([1.0, 1.0, 1.0, 1.0], [1, 1, 0, 0]) == pytest.approx(0.5)


# —— 等序回归校准器 ——

def test_isotonic_pav_merges_violation():
    # 分数 [1,2,3,4] 对应经验率 [0,1,0,1]：PAV 把中间的倒挂并块成 0.5
    cal = IsotonicCalibrator().fit([1, 2, 3, 4], [0, 1, 0, 1])
    np.testing.assert_allclose(cal.predict([1, 2, 3, 4]), [0.0, 0.5, 0.5, 1.0])


def test_isotonic_monotone_non_decreasing():
    cal = IsotonicCalibrator().fit([1, 2, 3, 4], [0, 1, 0, 1])
    p = cal.predict([1, 1.5, 2, 2.5, 3, 3.5, 4])
    assert np.all(np.diff(p) >= -1e-12)


def test_isotonic_clamps_out_of_range_scores():
    cal = IsotonicCalibrator().fit([1, 2, 3, 4], [0, 1, 0, 1])
    assert cal.predict([-100])[0] == 0.0   # 低于最小锚点 → 端点
    assert cal.predict([100])[0] == 1.0    # 高于最大锚点 → 端点
    p = cal.predict([-5, 2.5, 50])
    assert np.all((p >= 0.0) & (p <= 1.0))


def test_isotonic_aggregates_repeated_scores():
    cal = IsotonicCalibrator().fit([1, 1, 2, 2], [0, 1, 0, 1])
    np.testing.assert_allclose(cal.predict([1, 2]), [0.5, 0.5])


def test_isotonic_predict_before_fit_raises():
    with pytest.raises(RuntimeError):
        IsotonicCalibrator().predict([1, 2, 3])


def test_isotonic_reproducible():
    scores = [0.1, 0.1, 0.9, 0.9, 0.5, 0.5, 0.3, 0.7]
    outcomes = [0, 0, 1, 0, 1, 0, 0, 1]
    c1 = IsotonicCalibrator().fit(scores, outcomes)
    c2 = IsotonicCalibrator().fit(scores, outcomes)
    np.testing.assert_array_equal(c1.predict(scores), c2.predict(scores))


def test_isotonic_not_worse_than_raw_brier():
    # 等序回归在拟合集上最小化单调映射的平方误差，故 Brier ≤ 直接用原始分数当概率
    scores = [0.1, 0.1, 0.9, 0.9, 0.5, 0.5, 0.3, 0.7]
    outcomes = [0, 0, 1, 0, 1, 0, 0, 1]
    cal = IsotonicCalibrator().fit(scores, outcomes)
    raw_brier = brier_score(scores, outcomes)
    cal_brier = brier_score(cal.predict(scores), outcomes)
    assert cal_brier <= raw_brier + 1e-12


# —— 校准/测试隔离 ——

def test_calibrate_and_evaluate_isolated():
    res = calibrate_and_evaluate([1, 2, 3, 4], [0, 0, 1, 1], [1, 2, 3, 4], [0, 0, 1, 1], n_bins=5)
    assert set(res) >= {"brier", "log_loss", "ece", "reliability", "n_cal", "n_test"}
    assert res["n_cal"] == 4 and res["n_test"] == 4
    assert res["brier"] == pytest.approx(0.0)  # 完美单调映射，测试集完全可分
    assert isinstance(res["reliability"], list) and len(res["reliability"]) == 5
