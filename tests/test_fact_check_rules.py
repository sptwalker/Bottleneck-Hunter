"""`chain/fact_check.py` 的规则回归哨兵。

这个模块的 `demo()` 里堆了五个案例（硬矛盾 / 无数据不误杀 / 同向 supported /
健康公司不被误降级 / 同字段反向主张各自计分），但 `if __name__ == "__main__"`
意味着**没有任何测试在跑它**——`tests/test_fact_check.py` 是另一套东西
（提示词防火墙与来源校验），与此模块无关。于是这些哨兵长年只在手工执行时才生效。

本文件把那五个案例接进测试套件：每个都是"改回去就静默出错"的行为，
尤其 Case 4（方向写反 → 说真话受罚）与 Case 5（去重键写窄 → 漏判高估）。
"""

from __future__ import annotations

import logging

from bottleneck_hunter.chain.fact_check import check_scorecard, demo


def test_demo_cases_all_pass(caplog):
    """全部五个案例的断言。任何一个被改回去，这里就红。"""
    with caplog.at_level(logging.WARNING):
        demo()  # 内部全是 assert，失败即抛 AssertionError


class TestDirectionAwareDedup:
    """Case 5 单独再钉一遍，附上"为什么"——避免后人误把去重键改回单字段。"""

    def _card(self, strengths, weaknesses, pe=18.0):
        from bottleneck_hunter.chain.models import (
            FinancialSnapshot,
            MarketRegion,
            SupplierInfo,
            SupplierScorecard,
        )

        return SupplierScorecard(
            supplier=SupplierInfo(
                name="测试E", ticker="000005.SZ", market=MarketRegion.A_STOCK,
                sector="测试行业", description="测试公司E",
            ),
            bottleneck_node="测试环节",
            market_position=6.0, customer_validation=6.0, capacity_status=6.0,
            financial_health=6.0, valuation=6.0, overall_score=6.0,
            strengths=strengths, weaknesses=weaknesses,
            financial_snapshot=FinancialSnapshot(
                data_source="test", report_date="2025-12-31", consensus_pe=pe,
            ),
        )

    def test_opposite_directions_on_same_field_both_counted(self):
        """同字段、反方向 = 两条独立主张，都要计分。

        只按 `actual_field` 去重时，"估值被高估"会落进 duplicate_skipped，
        它的 mismatch 计数一并消失（实测 mismatch 由 1 变 0）。
        """
        rep = check_scorecard(self._card(strengths=["估值便宜"], weaknesses=["估值被高估"]), None)
        mismatches = [f for f in rep.findings if f.verdict == "mismatch"]
        assert len(mismatches) == 1, [(f.data_field, f.verdict) for f in rep.findings]
        assert "duplicate_skipped" not in {f.verdict for f in rep.findings}

    def test_same_direction_on_same_field_still_deduped(self):
        """同字段、同方向才是真重复——去重必须保留，否则回到"一次观察计两分"。"""
        from bottleneck_hunter.chain.models import (
            FinancialSnapshot,
            FinancialTrend,
            MarketRegion,
            SupplierInfo,
            SupplierScorecard,
        )

        sc = SupplierScorecard(
            supplier=SupplierInfo(
                name="测试F", ticker="000006.SZ", market=MarketRegion.A_STOCK,
                sector="测试行业", description="测试公司F",
            ),
            bottleneck_node="测试环节",
            market_position=6.0, customer_validation=6.0, capacity_status=6.0,
            financial_health=6.0, valuation=6.0, overall_score=6.0,
            # financial_health 与 gross_margin_trend 都映射到 gross_margin_trend，同为 positive
            strengths=["财务稳健", "毛利率提升"],
            weaknesses=[],
            financial_snapshot=FinancialSnapshot(
                data_source="test", report_date="2025-12-31",
                trend=FinancialTrend(gross_margin_trend=5.0),
            ),
        )
        rep = check_scorecard(sc, None)
        verdicts = [f.verdict for f in rep.findings]
        assert verdicts.count("supported") == 1, verdicts
        assert verdicts.count("duplicate_skipped") == 1, verdicts
