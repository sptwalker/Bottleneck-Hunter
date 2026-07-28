"""可投性预筛选过滤器。

在供应商详细评估之前，用硬性规则快速淘汰不适合投资的候选标的。
规则包括：市场规模（TAM）、毛利率、日均成交额、上市时间。
每条规则的阈值均可通过参数覆盖；数据缺失时跳过对应规则（不因缺少数据而误杀）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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
        min_daily_amount_astock_wan: A股 最低日均成交额（万元）。默认 5000（即 ¥5000万/日）。
        min_daily_amount_us_wan: 美股 最低日均成交额（万美元）。默认 500（即 $5M/日）。
        min_listing_days: 最短上市天数。默认 365（约 1 年）。
    """

    def __init__(
        self,
        min_market_cap: float = 15.0,
        min_gross_margin: float = 20.0,
        min_daily_amount_astock_wan: float = 5000.0,
        min_daily_amount_us_wan: float = 500.0,
        min_listing_days: int = 365,
    ):
        self.min_market_cap = min_market_cap
        self.min_gross_margin = min_gross_margin
        self.min_daily_amount_astock_wan = min_daily_amount_astock_wan
        self.min_daily_amount_us_wan = min_daily_amount_us_wan
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

        # ---- 规则 3: 日均成交额（流动性，本币万元，市场感知阈值） ----
        avg_amt = financial.avg_daily_amount_wan if financial else None
        if avg_amt is not None:
            is_a = supplier.market == "a_stock"
            min_amt = self.min_daily_amount_astock_wan if is_a else self.min_daily_amount_us_wan
            unit = "万元" if is_a else "万美元"
            scores["daily_amount_wan"] = avg_amt
            if avg_amt < min_amt:
                reasons.append(
                    f"流动性不足 (日均成交额 {avg_amt:.0f}{unit} < 阈值 {min_amt:.0f}{unit})"
                )
        else:
            scores["daily_amount_wan"] = "N/A"

        # ---- 规则 4: 上市时间 ----
        days_since_ipo: int | None = None
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


if __name__ == "__main__":
    # ponytail: 流动性闸门自检 — 缺失跳过 / 低于阈值淘汰 / 达标通过（A股/美股各一遍）
    f = InvestabilityFilter()
    sup_a = SupplierInfo(name="测A", ticker="600000", market="a_stock",
                         market_cap=100.0, sector="金融", description="")
    sup_us = SupplierInfo(name="TestUS", ticker="AAA", market="us_stock",
                          market_cap=100.0, sector="tech", description="")
    assert f.check(sup_a, FinancialSnapshot()).passed, "缺失应跳过不误杀"
    r = f.check(sup_a, FinancialSnapshot(avg_daily_amount_wan=100.0))
    assert not r.passed and any("流动性" in x for x in r.reasons), r.reasons
    assert f.check(sup_a, FinancialSnapshot(avg_daily_amount_wan=8000.0)).passed, "A股达标应通过"
    assert not f.check(sup_us, FinancialSnapshot(avg_daily_amount_wan=100.0)).passed, "美股<$5M应淘汰"
    assert f.check(sup_us, FinancialSnapshot(avg_daily_amount_wan=900.0)).passed, "美股达标应通过"
    print("investability liquidity gate OK")
