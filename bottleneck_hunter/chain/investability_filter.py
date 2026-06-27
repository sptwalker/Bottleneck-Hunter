"""可投性预筛选过滤器。

在供应商详细评估之前，用硬性规则快速淘汰不适合投资的候选标的。
规则包括：市场规模（TAM）、毛利率、日均成交额、上市时间。
每条规则的阈值均可通过参数覆盖；数据缺失时跳过对应规则（不因缺少数据而误杀）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from bottleneck_hunter.chain.models import FinancialSnapshot, SupplierInfo

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """可投性过滤结果。"""

    passed: bool
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, object] = field(default_factory=dict)


class InvestabilityFilter:
    """可投性硬性规则过滤器。

    在供应商评估之前运行，快速淘汰不符合条件的候选。
    每条规则检查数据字段，数据缺失时自动跳过该规则。

    Args:
        min_market_cap: 最低市值阈值（A 股单位: 亿；美股单位: $B）。默认 15 亿 / $1.5B。
        min_gross_margin: 最低毛利率阈值 (%)。默认 20。
        min_daily_volume: 最低日均成交额 ($K)。默认 500（即 $500K）。
        min_listing_days: 最短上市天数。默认 365（约 1 年）。
    """

    def __init__(
        self,
        min_market_cap: float = 15.0,
        min_gross_margin: float = 20.0,
        min_daily_volume: float = 500.0,
        min_listing_days: int = 365,
    ):
        self.min_market_cap = min_market_cap
        self.min_gross_margin = min_gross_margin
        self.min_daily_volume = min_daily_volume
        self.min_listing_days = min_listing_days

    def check(
        self,
        supplier: SupplierInfo,
        financial: FinancialSnapshot | None = None,
    ) -> FilterResult:
        """对单个供应商执行可投性检查。

        Args:
            supplier: 候选供应商信息
            financial: 财务数据快照（可选）

        Returns:
            FilterResult: passed=True 表示通过，passed=False 表示被淘汰
        """
        reasons: list[str] = []
        scores: dict[str, object] = {}

        # ---- 规则 1: 市场规模（TAM / 市值代理） ----
        market_cap = supplier.market_cap
        if market_cap is not None:
            scores["market_cap"] = market_cap
            if market_cap < self.min_market_cap:
                reasons.append(
                    f"天花板太低 (市值 {market_cap:.1f} < 阈值 {self.min_market_cap})"
                )
        else:
            scores["market_cap"] = "N/A"

        # ---- 规则 2: 毛利率 ----
        # 优先使用 FinancialSnapshot 的真实数据，其次用 SupplierInfo
        gross_margin = None
        if financial and financial.gross_margin_pct is not None:
            gross_margin = financial.gross_margin_pct
        elif supplier.gross_margin is not None:
            gross_margin = supplier.gross_margin

        if gross_margin is not None:
            scores["gross_margin"] = gross_margin
            if gross_margin < self.min_gross_margin:
                reasons.append(
                    f"定价权弱 (毛利率 {gross_margin:.1f}% < 阈值 {self.min_gross_margin}%)"
                )
        else:
            scores["gross_margin"] = "N/A"

        # ---- 规则 3: 日均成交额 ----
        # 当前数据模型中没有直接的日均成交额字段，
        # 如果未来添加了该字段，可在此处检查。
        # 目前跳过（数据缺失不误杀）。
        avg_daily_volume: Optional[float] = None
        if avg_daily_volume is not None:
            scores["daily_volume_k"] = avg_daily_volume
            if avg_daily_volume < self.min_daily_volume:
                reasons.append(
                    f"流动性不足 (日均成交额 ${avg_daily_volume:.0f}K < 阈值 ${self.min_daily_volume:.0f}K)"
                )
        else:
            scores["daily_volume_k"] = "N/A"

        # ---- 规则 4: 上市时间 ----
        days_since_ipo: Optional[int] = None
        if financial and financial.days_since_ipo is not None:
            days_since_ipo = financial.days_since_ipo

        if days_since_ipo is not None:
            scores["days_since_ipo"] = days_since_ipo
            if days_since_ipo < self.min_listing_days:
                reasons.append(
                    f"信息不充分 (上市 {days_since_ipo} 天 < 阈值 {self.min_listing_days} 天)"
                )
        else:
            scores["days_since_ipo"] = "N/A"

        passed = len(reasons) == 0
        if not passed:
            logger.info(
                f"可投性过滤淘汰: {supplier.name} ({supplier.ticker}) — {'; '.join(reasons)}"
            )

        return FilterResult(passed=passed, reasons=reasons, scores=scores)

    def filter_batch(
        self,
        suppliers: list[SupplierInfo],
        financial_map: dict[str, FinancialSnapshot] | None = None,
    ) -> tuple[list[SupplierInfo], list[tuple[SupplierInfo, FilterResult]]]:
        """批量过滤供应商列表。

        Args:
            suppliers: 候选供应商列表
            financial_map: ticker -> FinancialSnapshot 映射

        Returns:
            (通过的供应商列表, 被淘汰的 (供应商, 过滤结果) 列表)
        """
        financial_map = financial_map or {}
        passed: list[SupplierInfo] = []
        rejected: list[tuple[SupplierInfo, FilterResult]] = []

        for supplier in suppliers:
            financial = financial_map.get(supplier.ticker)
            result = self.check(supplier, financial)
            if result.passed:
                passed.append(supplier)
            else:
                rejected.append((supplier, result))

        if rejected:
            logger.info(
                f"可投性过滤: {len(passed)} 家通过, {len(rejected)} 家淘汰 "
                f"(共 {len(suppliers)} 家)"
            )

        return passed, rejected
