"""P1-1 概率化信号与校准。

把原始信号分数映射为校准概率，并度量校准质量：
- Brier / LogLoss：概率预测的整体误差（越小越好）。
- ECE + 可靠性曲线：分桶比较"预测概率"与"实际频率"，量化过/欠自信。
- `IsotonicCalibrator`：等序回归把单调的原始分数映射为校准概率；只在校准集 fit、
  在独立测试集 apply，从结构上避免用测试集信息调参（校准/测试隔离）。

与 `model_calibrator.py` 明确分层：后者是 AI 模型共识"准确率 → 权重"的加权，本模块是
"原始分数 → 校准概率"的概率校准，两者互不依赖。不引入 scipy；等序回归用 PAV
（pool adjacent violators），纯 numpy 确定可复现。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _validate_binary(outcomes) -> np.ndarray:
    y = np.asarray(outcomes, dtype=float)
    if y.size == 0:
        raise ValueError("需要至少一个样本")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("结果 outcomes 必须全为 0 或 1")
    return y


def _validate_probs_outcomes(probs, outcomes) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(probs, dtype=float)
    y = _validate_binary(outcomes)
    if p.shape != y.shape:
        raise ValueError("probs 与 outcomes 形状必须一致")
    if np.any((p < 0.0) | (p > 1.0)):
        raise ValueError("概率 probs 必须落在 [0, 1]")
    return p, y


def brier_score(probs, outcomes) -> float:
    """均方概率误差 mean((p - y)^2)，越小越好，完美=0、最差=1。"""
    p, y = _validate_probs_outcomes(probs, outcomes)
    return float(np.mean((p - y) ** 2))


def log_loss(probs, outcomes, *, eps: float = 1e-15) -> float:
    """对数损失 -mean(y·log p + (1-y)·log(1-p))；p 先钳到 [eps, 1-eps] 防溢出。"""
    p, y = _validate_probs_outcomes(probs, outcomes)
    p = np.clip(p, eps, 1.0 - eps)
    return float(-np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    count: int
    mean_pred: float   # 桶内预测概率均值（置信度）；空桶为 nan
    empirical: float   # 桶内实际正例率（准确率）；空桶为 nan


def reliability_curve(probs, outcomes, *, n_bins: int = 10) -> list[ReliabilityBin]:
    """把 [0,1] 等宽分成 n_bins 桶，返回每桶的预测均值与实际频率（可靠性图数据）。"""
    p, y = _validate_probs_outcomes(probs, outcomes)
    if n_bins <= 0:
        raise ValueError("n_bins 必须为正整数")
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        # 末桶取闭区间以纳入 p==1.0，其余为左闭右开。
        mask = (p >= lo) & (p <= hi) if i == n_bins - 1 else (p >= lo) & (p < hi)
        cnt = int(mask.sum())
        if cnt == 0:
            bins.append(ReliabilityBin(lo, hi, 0, float("nan"), float("nan")))
        else:
            bins.append(ReliabilityBin(lo, hi, cnt, float(p[mask].mean()), float(y[mask].mean())))
    return bins


def expected_calibration_error(probs, outcomes, *, n_bins: int = 10) -> float:
    """ECE：各桶 |实际频率 - 预测均值| 按样本占比加权求和，越小越校准。"""
    p, y = _validate_probs_outcomes(probs, outcomes)
    n = p.size
    ece = 0.0
    for b in reliability_curve(p, y, n_bins=n_bins):
        if b.count:
            ece += (b.count / n) * abs(b.empirical - b.mean_pred)
    return float(ece)


def _pav(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Pool Adjacent Violators：加权等序回归，返回与输入等长的非降拟合值。"""
    block_v: list[float] = []
    block_w: list[float] = []
    block_n: list[int] = []
    for val, wt in zip(values.astype(float), weights.astype(float), strict=True):
        cv, cw, cn = float(val), float(wt), 1
        while block_v and block_v[-1] > cv:  # 仅在严格违反单调时并块
            pv, pw, pn = block_v.pop(), block_w.pop(), block_n.pop()
            cv = (pv * pw + cv * cw) / (pw + cw)
            cw += pw
            cn += pn
        block_v.append(cv)
        block_w.append(cw)
        block_n.append(cn)
    out = np.empty(values.size, dtype=float)
    pos = 0
    for bv, bn in zip(block_v, block_n, strict=True):
        out[pos:pos + bn] = bv
        pos += bn
    return out


@dataclass
class IsotonicCalibrator:
    """等序回归概率校准器：单调地把原始分数映射为校准概率 [0,1]。

    只在校准集 fit；predict 用分段线性插值，越界钳制到端点。相同输入必得相同映射。
    """

    _x: np.ndarray | None = None  # 拟合锚点分数（升序、去重）
    _y: np.ndarray | None = None  # 对应校准概率（非降、[0,1]）

    def fit(self, scores, outcomes) -> IsotonicCalibrator:
        s = np.asarray(scores, dtype=float)
        y = _validate_binary(outcomes)
        if s.shape != y.shape:
            raise ValueError("scores 与 outcomes 形状必须一致")
        # 先按唯一分数聚合正例率与样本数，再做加权等序回归。
        ux, inv = np.unique(s, return_inverse=True)
        inv = np.ravel(inv)
        counts = np.bincount(inv).astype(float)
        rates = np.bincount(inv, weights=y) / counts
        self._x = ux
        self._y = np.clip(_pav(rates, counts), 0.0, 1.0)
        return self

    def predict(self, scores) -> np.ndarray:
        if self._x is None or self._y is None:
            raise RuntimeError("IsotonicCalibrator 必须先 fit 再 predict")
        s = np.asarray(scores, dtype=float)
        return np.clip(np.interp(s, self._x, self._y), 0.0, 1.0)


def calibrate_and_evaluate(
    cal_scores, cal_outcomes, test_scores, test_outcomes, *, n_bins: int = 10,
) -> dict:
    """校准/测试隔离：只在校准集 fit，在独立测试集 apply 并度量校准质量。"""
    calibrator = IsotonicCalibrator().fit(cal_scores, cal_outcomes)
    test_probs = calibrator.predict(test_scores)
    return {
        "brier": brier_score(test_probs, test_outcomes),
        "log_loss": log_loss(test_probs, test_outcomes),
        "ece": expected_calibration_error(test_probs, test_outcomes, n_bins=n_bins),
        "reliability": reliability_curve(test_probs, test_outcomes, n_bins=n_bins),
        "n_cal": int(np.asarray(cal_scores, dtype=float).size),
        "n_test": int(test_probs.size),
    }
