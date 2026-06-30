"""滑点模型 — 模拟真实市场的交易摩擦成本

买入时价格上调、卖出时价格下调，幅度取决于：
- 基础滑点 (base_bps)：流动性基准
- 成交量冲击 (impact_factor)：大单占日均量比例越大，滑点越大
"""

from __future__ import annotations

import math

SLIPPAGE_CONFIG = {
    "us_stock": {"base_bps": 10, "impact_factor": 0.5, "min_bps": 5, "max_bps": 100},
    "a_stock":  {"base_bps": 15, "impact_factor": 0.8, "min_bps": 8, "max_bps": 150},
}


def calc_slippage(
    price: float,
    shares: int,
    side: str,
    market: str = "us_stock",
    avg_volume: int | None = None,
) -> tuple[float, float]:
    """计算滑点调整后的成交价。

    Returns:
        (adjusted_price, slippage_bps) — 调整后价格 和 实际滑点基点数
    """
    cfg = SLIPPAGE_CONFIG.get(market, SLIPPAGE_CONFIG["us_stock"])
    base_bps = cfg["base_bps"]

    impact_bps = 0.0
    if avg_volume and avg_volume > 0 and shares > 0:
        participation = shares / avg_volume
        impact_bps = cfg["impact_factor"] * math.sqrt(participation) * 10000

    total_bps = base_bps + impact_bps
    total_bps = max(cfg["min_bps"], min(total_bps, cfg["max_bps"]))

    slip_pct = total_bps / 10000

    if side in ("buy", "add"):
        adjusted = round(price * (1 + slip_pct), 4)
    elif side in ("sell", "reduce"):
        adjusted = round(price * (1 - slip_pct), 4)
    else:
        adjusted = price
        total_bps = 0.0

    return adjusted, round(total_bps, 2)
