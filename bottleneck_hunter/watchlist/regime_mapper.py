"""L1→L2 量化映射表 — 将 L1 宏观研判的定性标签映射为 L2 仓位量化区间

消除 LLM 在 regime→仓位映射环节的随意性，建立确定性规则。
"""

from __future__ import annotations

REGIME_MAP: dict[tuple[str, str], dict] = {
    # (regime, risk_appetite) → 量化约束
    ("bull", "aggressive"):    {"equity_pct": (70, 85), "cash_pct": (10, 20), "max_single_pct": 20, "beta_limit": 1.5},
    ("bull", "balanced"):      {"equity_pct": (60, 75), "cash_pct": (15, 25), "max_single_pct": 15, "beta_limit": 1.3},
    ("bull", "defensive"):     {"equity_pct": (40, 60), "cash_pct": (25, 40), "max_single_pct": 12, "beta_limit": 1.0},

    ("sideways", "aggressive"): {"equity_pct": (50, 70), "cash_pct": (20, 35), "max_single_pct": 15, "beta_limit": 1.2},
    ("sideways", "balanced"):   {"equity_pct": (40, 60), "cash_pct": (25, 40), "max_single_pct": 12, "beta_limit": 1.0},
    ("sideways", "defensive"):  {"equity_pct": (25, 45), "cash_pct": (35, 55), "max_single_pct": 10, "beta_limit": 0.8},

    ("bear", "aggressive"):    {"equity_pct": (30, 50), "cash_pct": (35, 50), "max_single_pct": 10, "beta_limit": 0.8},
    ("bear", "balanced"):      {"equity_pct": (20, 40), "cash_pct": (40, 60), "max_single_pct": 8,  "beta_limit": 0.7},
    ("bear", "defensive"):     {"equity_pct": (10, 25), "cash_pct": (50, 70), "max_single_pct": 5,  "beta_limit": 0.5},
}

_REGIME_ALIASES = {
    "bullish": "bull",
    "bearish": "bear",
    "neutral": "sideways",
    "range_bound": "sideways",
    "transition": "sideways",
    "volatile": "sideways",
}


def _normalize_regime(regime: str) -> str:
    r = regime.lower().strip()
    return _REGIME_ALIASES.get(r, r)


def get_allocation_bounds(
    regime: str,
    risk_appetite: str,
    confidence: float = 5.0,
) -> dict:
    """根据 L1 输出返回仓位区间和约束参数。

    Args:
        regime: L1 的 regime 标签 (bull/sideways/bear/transition)
        risk_appetite: L1 的 risk_appetite (aggressive/balanced/defensive)
        confidence: L1 的 regime_confidence (1-10)，越高越靠近区间上界

    Returns:
        dict with keys: equity_min, equity_max, cash_min, cash_max,
                        max_single_pct, beta_limit, confidence_weight
    """
    norm_regime = _normalize_regime(regime)
    key = (norm_regime, risk_appetite.lower().strip())
    bounds = REGIME_MAP.get(key)

    if not bounds:
        bounds = REGIME_MAP.get((norm_regime, "balanced"))
    if not bounds:
        bounds = REGIME_MAP[("sideways", "balanced")]

    conf_weight = max(0.0, min(1.0, (confidence - 1) / 9))

    eq_range = bounds["equity_pct"]
    cash_range = bounds["cash_pct"]

    # 确定性插值：根据 confidence_weight 计算推荐仓位值
    recommended_equity = round(eq_range[0] + (eq_range[1] - eq_range[0]) * conf_weight)
    recommended_cash = round(cash_range[1] - (cash_range[1] - cash_range[0]) * conf_weight)

    return {
        "equity_min": eq_range[0],
        "equity_max": eq_range[1],
        "recommended_equity": recommended_equity,
        "cash_min": cash_range[0],
        "cash_max": cash_range[1],
        "recommended_cash": recommended_cash,
        "max_single_pct": bounds["max_single_pct"],
        "beta_limit": bounds["beta_limit"],
        "confidence_weight": round(conf_weight, 2),
        "regime": norm_regime,
        "risk_appetite": risk_appetite.lower().strip(),
    }


def format_bounds_for_prompt(bounds: dict) -> str:
    """将映射结果格式化为可插入 prompt 的文本"""
    return (
        f"## 量化仓位约束（由 L1 宏观研判自动生成，必须遵守）\n"
        f"- 当前市场状态: {bounds['regime']} | 风险偏好: {bounds['risk_appetite']}\n"
        f"- 权益仓位区间: {bounds['equity_min']}% ~ {bounds['equity_max']}%\n"
        f"- 推荐权益仓位: {bounds['recommended_equity']}%（基于 L1 置信度 {bounds['confidence_weight']} 插值）\n"
        f"- 现金保留区间: {bounds['cash_min']}% ~ {bounds['cash_max']}%\n"
        f"- 推荐现金仓位: {bounds['recommended_cash']}%\n"
        f"- 单只股票上限: {bounds['max_single_pct']}%\n"
        f"- 组合 Beta 上限: {bounds['beta_limit']}\n\n"
        f"**硬性要求**: target_allocation 中的 equity_pct 必须在 "
        f"{bounds['equity_min']}% ~ {bounds['equity_max']}% 范围内，"
        f"建议设为 {bounds['recommended_equity']}%。"
    )
