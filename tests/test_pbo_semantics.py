"""PBO（CSCV）与 DSR 的语义守护。

本文件不重测公式，而是**钉死口径**——防止日后有人把"简化版"当成完整 CSCV，
或把 PBO 误读成"策略能不能赚钱"。
"""

from __future__ import annotations

import numpy as np
import pytest

from bottleneck_hunter.watchlist.evaluation import deflated_sharpe, overfit_flags, pbo_cscv


def _matrix(seed: int = 0, t: int = 64, n: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0.001, 0.02, size=(t, n))


def test_pbo_uses_full_cscv_not_fold_split():
    """完整 CSCV 应按时间切 S 块并枚举对半组合。

    可观测判据：块数变化会改变结果，说明它真的在切块而非沿用外部 fold。
    """
    m = _matrix(seed=7)
    four = pbo_cscv(m, n_splits=4)
    eight = pbo_cscv(m, n_splits=8)
    assert 0.0 <= four <= 1.0
    assert 0.0 <= eight <= 1.0
    assert four != eight, "不同切块数应得到不同 PBO；相同说明没真在枚举组合"


def test_pbo_rejects_insufficient_sample():
    """样本不足必须抛错，不能退化成猜一个数。"""
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros((4, 3)), n_splits=16)
    with pytest.raises(ValueError):
        pbo_cscv(np.zeros((64, 1)), n_splits=8)  # N<2


def test_pbo_pure_noise_is_high():
    """纯噪声下"样本内最优"没有外推能力，PBO 应接近/超过 0.5。"""
    m = _matrix(seed=11, t=64, n=8)
    assert pbo_cscv(m, n_splits=8) >= 0.4


def test_pbo_stable_ordering_is_low():
    """若某配置在所有块上都稳定占优，PBO 应显著低。"""
    t, n = 64, 5
    base = np.full((t, n), 0.0)
    base[:, 0] = 0.05  # 配置 0 恒强
    base[:, 1:] = 0.0
    assert pbo_cscv(base, n_splits=8) <= 0.2


def test_ties_counted_conservatively():
    """全并列时没有配置"严格胜过"别人，PBO 应偏悲观/保守而非报 0。"""
    m = np.zeros((64, 4))
    assert pbo_cscv(m, n_splits=8) == pytest.approx(1.0)


def test_dsr_needs_enough_periods():
    with pytest.raises(ValueError):
        deflated_sharpe([0.01, 0.02], [0.1, 0.2])


def test_dsr_decreases_with_more_trials():
    """试验次数越多，同样的夏普越不显著——这是 DSR 存在的意义。"""
    rng = np.random.default_rng(3)
    returns = rng.normal(0.004, 0.01, size=250)
    few = deflated_sharpe(returns, [0.4, 0.2, 0.1])
    many = deflated_sharpe(returns, [0.4 - 0.01 * i for i in range(50)])
    assert many < few


def test_overfit_flags_semantics():
    """标记只提示，不替人下结论。"""
    assert overfit_flags(pbo=None, dsr=None)["suspected_overfit"] is False
    assert overfit_flags(pbo=0.9, dsr=None)["suspected_overfit"] is True
    assert overfit_flags(pbo=None, dsr=0.2)["suspected_overfit"] is True
