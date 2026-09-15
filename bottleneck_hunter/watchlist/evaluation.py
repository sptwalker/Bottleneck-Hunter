"""P0-6 walk-forward / 样本外 / 消融 / 统计区间评估。

在事件驱动回测（P0-5）之上做严格样本外（out-of-sample）评估：
- `walk_forward_splits`：按时间滚动切分 train/test 窗口，test 段严格晚于 train 段，
  从结构上杜绝未来数据泄漏（训练窗绝不含测试期之后的信息）。
- `bootstrap_ci`：对任意统计量做有放回重采样置信区间；种子固定 → 相同输入必得相同区间。
- `ablation`：基线 vs 变体的统计量差 + 差值 bootstrap 区间，用区间是否跨 0 判定增量是否显著，
  证明或否定 LLM / 多 Glob 的真实增量（供 P2-3 复用）。
- `summarize_oos`：把逐窗样本外收益聚合成标准指标（均值 / 年化 / 夏普）并附 bootstrap 区间。

不引入 scipy；bootstrap 用 numpy 固定种子生成器，确定可复现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Split:
    """一次 walk-forward 切分：训练窗与紧随其后的样本外测试窗（日期，已排序）。"""

    train: tuple[str, ...]
    test: tuple[str, ...]


@dataclass(frozen=True)
class CI:
    """点估计 + 置信区间。"""

    point: float
    low: float
    high: float
    confidence: float

    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0


@dataclass(frozen=True)
class AblationResult:
    """消融对比结果：变体相对基线的统计量差及其显著性。"""

    delta: float  # statistic(variant) - statistic(baseline)
    ci: CI
    significant: bool  # 差值区间不跨 0
    direction: str  # 'positive' | 'negative' | 'inconclusive'


def walk_forward_splits(
    dates: Sequence[str],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
) -> list[Split]:
    """按时间滚动切分 train/test；test 段严格晚于 train 段。

    dates 会先去重排序；train_size/test_size 为窗口内交易日数量；step 默认 = test_size
    （测试窗不重叠）。任一窗口若出现 train 最大日期 >= test 最小日期立即抛错（防泄漏）。
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size 与 test_size 必须为正整数")
    if step is not None and step <= 0:
        raise ValueError("step 必须为正整数")
    ordered = sorted(set(dates))
    step = step or test_size
    splits: list[Split] = []
    start = 0
    while start + train_size + test_size <= len(ordered):
        train = tuple(ordered[start:start + train_size])
        test = tuple(ordered[start + train_size:start + train_size + test_size])
        # 结构性 PIT 断言：训练集绝不能触及测试期或其后的任何日期。
        if train[-1] >= test[0]:
            raise ValueError(f"walk-forward 窗口泄漏：train 末日 {train[-1]} 不早于 test 首日 {test[0]}")
        splits.append(Split(train=train, test=test))
        start += step
    return splits


def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[np.ndarray], float] = np.mean,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> CI:
    """对样本做有放回重采样，返回 statistic 的 bootstrap 置信区间（种子固定，可复现）。"""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        raise ValueError("bootstrap_ci 需要至少一个样本")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence 必须落在 (0, 1)")
    point = float(statistic(arr))
    if arr.size == 1:
        return CI(point=point, low=point, high=point, confidence=confidence)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_resamples, arr.size))
    stats = np.array([float(statistic(arr[row])) for row in idx])
    alpha = (1.0 - confidence) / 2.0
    low, high = np.quantile(stats, [alpha, 1.0 - alpha])
    return CI(point=point, low=float(low), high=float(high), confidence=confidence)


def ablation(
    baseline: Sequence[float],
    variant: Sequence[float],
    *,
    statistic: Callable[[np.ndarray], float] = np.mean,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> AblationResult:
    """基线 vs 变体：statistic(variant) - statistic(baseline) 的点估计与 bootstrap 区间。

    两序列等长时按配对差重采样（同一逐窗样本外收益），否则按各自独立重采样差。
    区间不跨 0 即判定增量显著；不显著返回 inconclusive，不夸大结论。
    """
    b = np.asarray(baseline, dtype=float)
    v = np.asarray(variant, dtype=float)
    if b.size == 0 or v.size == 0:
        raise ValueError("ablation 需要非空的基线与变体样本")
    point = float(statistic(v)) - float(statistic(b))
    rng = np.random.default_rng(seed)
    if b.size == v.size:
        diff = v - b
        idx = rng.integers(0, diff.size, size=(n_resamples, diff.size))
        stats = np.array([float(statistic(diff[row])) for row in idx])
    else:
        bi = rng.integers(0, b.size, size=(n_resamples, b.size))
        vi = rng.integers(0, v.size, size=(n_resamples, v.size))
        stats = np.array([float(statistic(v[vr])) - float(statistic(b[br]))
                          for br, vr in zip(bi, vi, strict=True)])
    alpha = (1.0 - confidence) / 2.0
    low, high = np.quantile(stats, [alpha, 1.0 - alpha])
    ci = CI(point=point, low=float(low), high=float(high), confidence=confidence)
    significant = ci.excludes_zero()
    direction = "inconclusive"
    if significant:
        direction = "positive" if point > 0 else "negative"
    return AblationResult(delta=point, ci=ci, significant=significant, direction=direction)


def summarize_oos(
    period_returns: Sequence[float],
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    confidence: float = 0.95,
    seed: int = 0,
) -> dict:
    """把逐期样本外收益聚合成标准指标 + bootstrap 区间。

    period_returns：每个样本外窗口（或每期）的简单收益率（如 0.012 = +1.2%）。
    返回均值、年化、夏普（无风险=0 的简化口径）及均值的置信区间。
    """
    arr = np.asarray(period_returns, dtype=float)
    if arr.size == 0:
        raise ValueError("summarize_oos 需要至少一期样本外收益")
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    sharpe = float(mean / std * np.sqrt(periods_per_year)) if std > 0 else 0.0
    ci = bootstrap_ci(arr, statistic=np.mean, confidence=confidence, seed=seed)
    return {
        "n_periods": int(arr.size),
        "mean_return": mean,
        "annualized_return": float((1.0 + mean) ** periods_per_year - 1.0),
        "volatility": std,
        "sharpe": sharpe,
        "mean_ci_low": ci.low,
        "mean_ci_high": ci.high,
        "confidence": confidence,
    }
