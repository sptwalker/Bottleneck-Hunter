"""P0-6 walk-forward / 样本外 / 消融 / 统计区间评估。

在事件驱动回测（P0-5）之上做严格样本外（out-of-sample）评估：
- `walk_forward_splits`：按时间滚动切分 train/test 窗口，test 段严格晚于 train 段，
  从结构上杜绝未来数据泄漏（训练窗绝不含测试期之后的信息）。
- `bootstrap_ci`：对任意统计量做有放回重采样置信区间；种子固定 → 相同输入必得相同区间。
- `ablation`：基线 vs 变体的统计量差 + 差值 bootstrap 区间，用区间是否跨 0 判定增量是否显著，
  证明或否定 LLM / 多 Glob 的真实增量（供 P2-3 复用）。
- `summarize_oos`：把逐窗样本外收益聚合成标准指标（均值 / 年化 / 夏普）并附 bootstrap 区间。

- `block_bootstrap_ci`：移动块 bootstrap，保留收益序列自相关（iid 重采样会低估区间宽度）。
- `pbo_cscv`：组合对称交叉验证的过拟合概率 PBO（Bailey et al. 2015）。
- `deflated_sharpe`：多重试验 + 非正态校正后的夏普显著性 DSR（Bailey & López de Prado 2014）。
- `overfit_flags`：PBO>0.5 或 DSR<0.9 → 疑似过拟合（只标记，绝不自动采纳）。

不引入 scipy（正态分布用 stdlib statistics.NormalDist）；bootstrap 用 numpy 固定种子生成器，确定可复现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist

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


def block_bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[np.ndarray], float] = np.mean,
    block: int | None = None,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> CI:
    """移动块 bootstrap：每次抽连续 block 长的片段拼接，保留序列自相关。block 默认 ≈ n^(1/3)。"""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        raise ValueError("block_bootstrap_ci 需要至少一个样本")
    point = float(statistic(arr))
    n = arr.size
    b = max(1, min(n, block or round(n ** (1 / 3))))
    if n == 1 or b == 1:
        return bootstrap_ci(arr, statistic=statistic, n_resamples=n_resamples, confidence=confidence, seed=seed)
    rng = np.random.default_rng(seed)
    k = -(-n // b)  # 向上取整的块数
    starts = rng.integers(0, n - b + 1, size=(n_resamples, k))
    offs = np.arange(b)
    stats = np.array([float(statistic(arr[(row[:, None] + offs).ravel()[:n]])) for row in starts])
    alpha = (1.0 - confidence) / 2.0
    low, high = np.quantile(stats, [alpha, 1.0 - alpha])
    return CI(point=point, low=float(low), high=float(high), confidence=confidence)


def _sharpe(x: np.ndarray) -> np.ndarray:
    """按列（或一维）算逐期夏普（不年化）；零波动 → 0。"""
    sd = x.std(axis=0, ddof=1)
    mu = x.mean(axis=0)
    return np.divide(mu, sd, out=np.zeros_like(mu, dtype=float), where=sd > 0)


def pbo_cscv(perf: Sequence[Sequence[float]], *, n_splits: int = 16) -> float:
    """CSCV 过拟合概率。perf: T×N 矩阵（T 期 × N 个试过的配置的逐期收益）。

    把 T 行切成 S 块，取所有 C(S, S/2) 种「半数做样本内 / 另一半做样本外」组合；
    每种组合下选样本内夏普最优的配置，看它在样本外的排名分位 ω，λ=ln(ω/(1-ω))。
    PBO = P(λ ≤ 0)＝样本内最优在样本外跌到中位数以下的频率。>0.5 ≈ 选优过程比抛硬币还差。
    """
    m = np.asarray(perf, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("pbo_cscv 需要 T×N 矩阵且 N≥2")
    s = n_splits - n_splits % 2
    if s < 2 or m.shape[0] < s * 2:
        raise ValueError(f"样本期 {m.shape[0]} 不足以切 {n_splits} 块（每块至少 2 期）")
    blocks = np.array_split(np.arange(m.shape[0]), s)
    n_cfg = m.shape[1]
    lams = []
    for ins in combinations(range(s), s // 2):
        is_idx = np.concatenate([blocks[i] for i in ins])
        oos_idx = np.concatenate([blocks[i] for i in range(s) if i not in ins])
        best = int(np.argmax(_sharpe(m[is_idx])))
        oos = _sharpe(m[oos_idx])
        rank = float((oos < oos[best]).sum() + 1)  # 1..N，越大越好
        w = rank / (n_cfg + 1)
        lams.append(np.log(w / (1 - w)))
    return float(np.mean(np.asarray(lams) <= 0))


_EULER_GAMMA = 0.5772156649015329


def deflated_sharpe(returns: Sequence[float], trial_sharpes: Sequence[float]) -> float:
    """DSR：所选策略逐期夏普在「试了 N 次取最好」+ 偏度/峰度校正后仍显著大于 0 的概率。

    returns：被选中策略的逐期收益；trial_sharpes：所有试过配置的逐期（非年化）夏普，N=len。
    SR₀ = √V[SR]·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))]——纯运气下 N 次试验的期望最大夏普。
    """
    r = np.asarray(returns, dtype=float)
    t = r.size
    trials = np.asarray(trial_sharpes, dtype=float)
    if t < 3:
        raise ValueError("deflated_sharpe 需要至少 3 期收益")
    sr = float(_sharpe(r))
    nd = NormalDist()
    n = trials.size
    if n >= 2:
        v = float(trials.var(ddof=1))
        sr0 = np.sqrt(v) * ((1 - _EULER_GAMMA) * nd.inv_cdf(1 - 1 / n) + _EULER_GAMMA * nd.inv_cdf(1 - 1 / (n * np.e)))
    else:
        sr0 = 0.0
    sd = r.std(ddof=0)
    z = (r - r.mean()) / sd if sd > 0 else np.zeros_like(r)
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())  # 非超额峰度，正态=3
    var_sr = max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2)
    return float(nd.cdf((sr - sr0) * np.sqrt(t - 1) / np.sqrt(var_sr)))


PBO_MAX = 0.5
DSR_MIN = 0.9


def overfit_flags(*, pbo: float | None = None, dsr: float | None = None) -> dict:
    """统一的过拟合判定：PBO>0.5 或 DSR<0.9 → suspected_overfit。只做标记，调用方不得据此自动采纳。"""
    reasons = []
    if pbo is not None and pbo > PBO_MAX:
        reasons.append(f"PBO={pbo:.2f}>{PBO_MAX}")
    if dsr is not None and dsr < DSR_MIN:
        reasons.append(f"DSR={dsr:.2f}<{DSR_MIN}")
    return {"pbo": pbo, "dsr": dsr, "suspected_overfit": bool(reasons),
            "label": "疑似过拟合" if reasons else "", "reasons": reasons}
