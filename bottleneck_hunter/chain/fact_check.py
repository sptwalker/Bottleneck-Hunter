"""FactCheck Gate — 确定性事实核查,替代多AI再打分的 cross_validation。

零 LLM 调用,纯算法比对:声称(strengths/weaknesses)与真实数据(财务/CR3/smart_money)
方向一致性检查。输出 credibility(0-10) + recommendation(PASS/REVIEW/REJECT) + findings。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bottleneck_hunter.chain.models import (
        SupplierScorecard,
        FinancialSnapshot,
        SmartMoneySignal,
        BottleneckReport,
    )

logger = logging.getLogger(__name__)


# ── 语义映射规则 ──────────────────────────────────────────────
# 格式: (声称关键词正则, 数据字段, 期望方向, 严重度)
# 期望方向: 'positive'正向(声称强就该数据高), 'negative'负向(声称弱就该数据低)
# 严重度: 'fatal' 硬矛盾直接REJECT, 'mismatch' 软不符-0.5分

_CLAIM_RULES = [
    # 财务健康类
    (r"(财务|盈利|利润)\s*(稳健|健康|优秀|强劲)", "financial_health", "positive", "fatal"),
    (r"毛利率?\s*(提升|增长|改善|扩张)", "gross_margin_trend", "positive", "mismatch"),
    (r"营收\s*(加速|高增|快速增长)", "revenue_acceleration", "positive", "mismatch"),
    (r"现金流\s*(充裕|健康|良好)", "cashflow_per_share", "positive", "fatal"),
    (r"负债率?\s*(低|健康|可控)", "debt_ratio_pct", "negative", "mismatch"),

    # 估值类
    (r"(估值|PE|市盈率)\s*(低|便宜|被低估|合理)", "consensus_pe", "negative", "mismatch"),
    (r"(高估|贵|泡沫)", "consensus_pe", "positive", "mismatch"),

    # 市场地位类
    (r"(龙头|龙一|第一|领先|市占率高)", "market_share", "positive", "mismatch"),
    (r"(垄断|寡头|集中度高)", "cr3_estimate", "positive", "mismatch"),

    # 聪明钱类
    (r"机构\s*(增持|看好|加仓)", "institution_holding_change", "positive", "mismatch"),
    (r"(北向|外资)\s*(流入|净买入)", "northbound_net_buy", "positive", "mismatch"),
    (r"(做空|沽空)\s*(压力|风险)", "short_interest_pct", "positive", "mismatch"),
]


class FactCheckFinding:
    """单条核查发现。"""

    def __init__(
        self,
        claim_text: str,
        rule_desc: str,
        data_field: str,
        data_value: float | None,
        expected_direction: str,
        actual_direction: str,
        severity: str,
        verdict: str,
    ):
        self.claim_text = claim_text
        self.rule_desc = rule_desc
        self.data_field = data_field
        self.data_value = data_value
        self.expected_direction = expected_direction
        self.actual_direction = actual_direction
        self.severity = severity
        self.verdict = verdict  # "fatal_contradiction" | "mismatch" | "supported" | "unverifiable"

    def to_dict(self) -> dict:
        return {
            "claim": self.claim_text,
            "rule": self.rule_desc,
            "field": self.data_field,
            "value": self.data_value,
            "expected": self.expected_direction,
            "actual": self.actual_direction,
            "severity": self.severity,
            "verdict": self.verdict,
        }


class FactCheckReport:
    """事实核查报告。"""

    def __init__(
        self,
        ticker: str,
        company_name: str,
        credibility: float,
        recommendation: str,
        findings: list[FactCheckFinding],
        summary: str = "",
    ):
        self.ticker = ticker
        self.company_name = company_name
        self.credibility = credibility  # 0-10
        self.recommendation = recommendation  # PASS | REVIEW | REJECT
        self.findings = findings
        self.summary = summary

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "credibility": self.credibility,
            "recommendation": self.recommendation,
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }


def check_scorecard(
    scorecard: SupplierScorecard,
    bottleneck_report: BottleneckReport | None = None,
) -> FactCheckReport:
    """对单个 scorecard 做事实核查。

    逻辑:
    1. 提取 strengths + weaknesses 文本
    2. 对每条规则,检查声称是否匹配
    3. 若匹配,查真实数据,判方向一致性
    4. 聚合成 credibility + recommendation
    """
    ticker = scorecard.supplier.ticker
    company_name = scorecard.supplier.name  # SupplierInfo.name not company_name

    # 拼接所有声称文本
    claims_text = " ".join(scorecard.strengths + scorecard.weaknesses)

    findings: list[FactCheckFinding] = []
    fatal_count = 0
    mismatch_count = 0
    supported_count = 0
    unverifiable_count = 0

    for pattern, field, expected_dir, severity in _CLAIM_RULES:
        match = re.search(pattern, claims_text)
        if not match:
            continue

        claim_snippet = match.group(0)

        # 获取真实数据
        data_value, actual_field = _get_data_value(scorecard, bottleneck_report, field)

        if data_value is None:
            # 数据缺失 → unverifiable,不罚分(保护小盘股)
            findings.append(FactCheckFinding(
                claim_text=claim_snippet,
                rule_desc=f"{field} 期望{expected_dir}",
                data_field=field,
                data_value=None,
                expected_direction=expected_dir,
                actual_direction="unknown",
                severity=severity,
                verdict="unverifiable",
            ))
            unverifiable_count += 1
            continue

        # 判断实际方向
        actual_dir = _judge_direction(actual_field, data_value, scorecard)

        # 比对方向
        if expected_dir == actual_dir:
            # 声称与数据一致 → supported
            findings.append(FactCheckFinding(
                claim_text=claim_snippet,
                rule_desc=f"{field} 期望{expected_dir}",
                data_field=field,
                data_value=data_value,
                expected_direction=expected_dir,
                actual_direction=actual_dir,
                severity=severity,
                verdict="supported",
            ))
            supported_count += 1
        else:
            # 方向相反
            if severity == "fatal":
                verdict = "fatal_contradiction"
                fatal_count += 1
            else:
                verdict = "mismatch"
                mismatch_count += 1

            findings.append(FactCheckFinding(
                claim_text=claim_snippet,
                rule_desc=f"{field} 期望{expected_dir}",
                data_field=field,
                data_value=data_value,
                expected_direction=expected_dir,
                actual_direction=actual_dir,
                severity=severity,
                verdict=verdict,
            ))

    # 聚合 credibility
    # 基础分10,每条fatal-3,每条mismatch-0.5,每条supported+0.2(上限1.0)
    credibility = 10.0
    credibility -= fatal_count * 3.0
    credibility -= mismatch_count * 0.5
    credibility += min(1.0, supported_count * 0.2)
    credibility = max(0.0, min(10.0, credibility))

    # recommendation
    if fatal_count > 0:
        recommendation = "REJECT"
    elif mismatch_count >= 3:
        recommendation = "REVIEW"
    else:
        recommendation = "PASS"

    # summary
    summary_parts = []
    if fatal_count > 0:
        summary_parts.append(f"{fatal_count}条硬矛盾")
    if mismatch_count > 0:
        summary_parts.append(f"{mismatch_count}条软不符")
    if supported_count > 0:
        summary_parts.append(f"{supported_count}条有数据支撑")
    if unverifiable_count > 0:
        summary_parts.append(f"{unverifiable_count}条无数据")
    summary = "、".join(summary_parts) if summary_parts else "未触发任何规则"

    return FactCheckReport(
        ticker=ticker,
        company_name=company_name,
        credibility=round(credibility, 1),
        recommendation=recommendation,
        findings=findings,
        summary=summary,
    )


def _get_data_value(
    scorecard: SupplierScorecard,
    bottleneck_report: BottleneckReport | None,
    field: str,
) -> tuple[float | None, str]:
    """从 scorecard 或 bottleneck_report 中提取指定字段的真实数据值。

    Returns: (value, actual_field_name)
    actual_field_name 用于 _judge_direction 正确识别数据类型。
    """
    snap = scorecard.financial_snapshot
    smart = scorecard.smart_money

    if field == "financial_health":
        # 用毛利率趋势+现金流作为代理
        if snap and snap.trend and snap.trend.gross_margin_trend is not None:
            return snap.trend.gross_margin_trend, "gross_margin_trend"
        return None, field

    if field == "gross_margin_trend":
        if snap and snap.trend:
            return snap.trend.gross_margin_trend, field
        return None, field

    if field == "revenue_acceleration":
        if snap and snap.trend:
            return snap.trend.revenue_acceleration, field
        return None, field

    if field == "cashflow_per_share":
        if snap:
            return snap.cashflow_per_share, field
        return None, field

    if field == "debt_ratio_pct":
        if snap:
            return snap.debt_ratio_pct, field
        return None, field

    if field == "consensus_pe":
        if snap:
            return snap.consensus_pe, field
        return None, field

    if field == "market_share":
        # 用 market_position 评分作为代理
        return scorecard.market_position, "market_position"

    if field == "cr3_estimate":
        if bottleneck_report:
            return bottleneck_report.cr3_estimate, field
        return None, field

    if field == "institution_holding_change":
        if smart:
            return smart.institution_holding_change, field
        return None, field

    if field == "northbound_net_buy":
        if smart:
            return smart.northbound_net_buy, field
        return None, field

    if field == "short_interest_pct":
        if smart:
            return smart.short_interest_pct, field
        return None, field

    return None, field


def _judge_direction(field: str, value: float, scorecard: SupplierScorecard) -> str:
    """根据字段名和数值,判断实际方向是 positive/negative/neutral。"""

    # 趋势类字段(百分点变化)
    if field in ("gross_margin_trend", "revenue_acceleration"):
        if value > 1.0:
            return "positive"
        elif value < -1.0:
            return "negative"
        else:
            return "neutral"

    # 现金流
    if field == "cashflow_per_share":
        if value is not None and value > 0:
            return "positive"
        else:
            return "negative"

    # 负债率(反向,越低越好)
    if field == "debt_ratio_pct":
        if value is not None and value < 50:
            return "positive"  # 低负债=好
        elif value is not None and value > 70:
            return "negative"
        else:
            return "neutral"

    # PE(反向,越低=估值便宜)
    if field == "consensus_pe":
        # 需要相对批次均值判断,这里简化用绝对值
        # TODO: 用批次分位数
        if value is not None and value < 20:
            return "positive"  # 低PE=便宜
        elif value is not None and value > 40:
            return "negative"  # 高PE=贵
        else:
            return "neutral"

    # 市场地位评分
    if field in ("market_share", "market_position"):
        if value >= 7.5:
            return "positive"
        elif value <= 4.0:
            return "negative"
        else:
            return "neutral"

    # CR3
    if field == "cr3_estimate":
        if value is not None and value >= 70:
            return "positive"  # 高集中度
        elif value is not None and value < 40:
            return "negative"
        else:
            return "neutral"

    # 机构持仓变化
    if field == "institution_holding_change":
        if value is not None and value > 5:
            return "positive"
        elif value is not None and value < -5:
            return "negative"
        else:
            return "neutral"

    # 北向资金
    if field == "northbound_net_buy":
        if value is not None and value > 0:
            return "positive"
        elif value is not None and value < 0:
            return "negative"
        else:
            return "neutral"

    # 做空比例
    if field == "short_interest_pct":
        if value is not None and value > 10:
            return "positive"  # 高做空=风险(与声称的"做空压力"同向)
        else:
            return "neutral"

    # 默认未知
    return "neutral"


def demo():
    """自测:构造假数据验证逻辑。"""
    from bottleneck_hunter.chain.models import (
        SupplierInfo,
        SupplierScorecard,
        FinancialSnapshot,
        FinancialTrend,
        MarketRegion,
    )

    # Case 1: 硬矛盾 — 声称"财务健康"但毛利趋势-5pp
    snap_bad = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=20.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=-5.0,  # 大幅下滑
            trend_summary="毛利率下滑",
        ),
        cashflow_per_share=-0.5,  # 现金流为负
    )
    sc_bad = SupplierScorecard(
        supplier=SupplierInfo(
            name="测试A",
            ticker="000001.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试行业",
            description="测试公司A",
        ),
        bottleneck_node="测试环节",
        market_position=8.0,
        customer_validation=7.0,
        capacity_status=6.0,
        financial_health=8.0,
        valuation=7.0,
        overall_score=7.5,
        strengths=["财务稳健", "现金流充裕"],  # 与数据矛盾
        weaknesses=[],
        financial_snapshot=snap_bad,
    )

    report1 = check_scorecard(sc_bad, None)
    assert report1.recommendation == "REJECT", f"预期REJECT,实际{report1.recommendation}"
    fatal_findings = [f for f in report1.findings if f.verdict == "fatal_contradiction"]
    assert len(fatal_findings) >= 1, f"预期至少1条fatal,实际{len(fatal_findings)}"
    logger.info("[demo] Case1通过: 硬矛盾正确触发REJECT")

    # Case 2: 无数据 → unverifiable,不误杀
    sc_no_data = SupplierScorecard(
        supplier=SupplierInfo(
            name="测试B",
            ticker="000002.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试行业",
            description="测试公司B",
        ),
        bottleneck_node="测试环节",
        market_position=7.0,
        customer_validation=6.0,
        capacity_status=6.0,
        financial_health=7.0,
        valuation=6.0,
        overall_score=6.5,
        strengths=["技术领先", "客户认可"],  # 定性声称,无数据可核
        weaknesses=[],
        financial_snapshot=None,
    )

    report2 = check_scorecard(sc_no_data, None)
    assert report2.recommendation != "REJECT", f"无数据不应REJECT,实际{report2.recommendation}"
    assert report2.credibility >= 9.0, f"无数据不应扣分,实际{report2.credibility}"
    logger.info("[demo] Case2通过: 无数据不误杀")

    # Case 3: 声称与数据同向 → supported
    snap_good = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=35.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=3.0,  # 提升
            revenue_acceleration=5.0,  # 加速
            trend_summary="持续增长",
        ),
        cashflow_per_share=2.5,
    )
    sc_good = SupplierScorecard(
        supplier=SupplierInfo(
            name="测试C",
            ticker="000003.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试行业",
            description="测试公司C",
        ),
        bottleneck_node="测试环节",
        market_position=9.0,
        customer_validation=8.0,
        capacity_status=8.0,
        financial_health=9.0,
        valuation=8.0,
        overall_score=8.5,
        strengths=["财务健康", "毛利率提升", "营收加速", "现金流充裕"],
        weaknesses=[],
        financial_snapshot=snap_good,
    )

    report3 = check_scorecard(sc_good, None)
    print(f"[DEBUG] Case3: credibility={report3.credibility}, rec={report3.recommendation}")
    print(f"[DEBUG] Findings count: {len(report3.findings)}")
    for f in report3.findings:
        print(f"  verdict={f.verdict} severity={f.severity} claim={f.claim_text!r} "
              f"field={f.data_field} value={f.data_value} exp={f.expected_direction} act={f.actual_direction}")

    assert report3.recommendation == "PASS", f"预期PASS,实际{report3.recommendation}"
    assert report3.credibility >= 9.5, f"全支撑应高分,实际{report3.credibility}"
    supported_findings = [f for f in report3.findings if f.verdict == "supported"]
    assert len(supported_findings) >= 3, f"预期>=3条supported,实际{len(supported_findings)}"
    logger.info("[demo] Case3通过: 声称与数据同向获supported")

    logger.info("[demo] ✓ 所有自测通过")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo()
