"""LLM-driven supplier evaluation.

Takes candidate SupplierInfo from the search step and evaluates each
against a bottleneck node, producing a SupplierScorecard.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    AlphaScore,
    BottleneckReport,
    FinancialSnapshot,
    SupplierInfo,
    SupplierScorecard,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _format_financial_block(snap: FinancialSnapshot) -> str:
    """将 FinancialSnapshot 格式化为 prompt 中可读的文本块。"""
    lines = [f"## 真实财务数据（来源: {snap.data_source}，报告期: {snap.report_date or '未知'}）"]
    fields = [
        ("营业总收入", snap.revenue_yi, "亿"),
        ("营收同比增速", snap.revenue_yoy_pct, "%"),
        ("归母净利润", snap.net_profit_yi, "亿"),
        ("净利润同比增速", snap.net_profit_yoy_pct, "%"),
        ("销售毛利率", snap.gross_margin_pct, "%"),
        ("净资产收益率(ROE)", snap.roe_pct, "%"),
        ("资产负债率", snap.debt_ratio_pct, "%"),
        ("每股经营现金流", snap.cashflow_per_share, ""),
    ]
    for label, val, unit in fields:
        if val is not None:
            lines.append(f"- {label}: {val}{unit}")
    analyst_lines = []
    if snap.analyst_report_count is not None:
        analyst_lines.append(f"近期研报覆盖: {snap.analyst_report_count} 篇")
    if snap.analyst_rating:
        analyst_lines.append(f"机构评级: {snap.analyst_rating}")
    if snap.consensus_eps is not None:
        analyst_lines.append(f"一致预期EPS: {snap.consensus_eps}")
    if snap.consensus_pe is not None:
        analyst_lines.append(f"一致预期PE: {snap.consensus_pe}")
    if analyst_lines:
        lines.append("- " + "，".join(analyst_lines))
    lines.append("\n⚠ 以上为市场API获取的真实数据，请优先基于这些数据进行财务健康和估值评分。")
    return "\n".join(lines)


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


class SupplierEvaluator:
    """Evaluate candidate suppliers using LLM."""

    LLM_TIMEOUT = 120
    MAX_CONCURRENCY = 4

    def __init__(
        self,
        llm: BaseChatModel,
        language: str = "zh",
    ):
        self.llm = llm
        self.language = language
        self._system_prompt = _load_prompt("supplier_eval")
        self._on_progress = None

    async def evaluate(
        self,
        supplier: SupplierInfo,
        bottleneck: BottleneckReport,
        financial_snapshot: FinancialSnapshot | None = None,
    ) -> SupplierScorecard:
        """Evaluate a single supplier against a bottleneck node."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        financial_block = ""
        if financial_snapshot:
            financial_block = "\n\n" + _format_financial_block(financial_snapshot)

        user_prompt = f"""{lang_note}

## 瓶颈环节
- 名称: {bottleneck.node_name}
- 描述: {bottleneck.node_description}
- 综合瓶颈得分: {bottleneck.overall_score}/10
- 关键洞察: {', '.join(bottleneck.key_insights)}

## 候选供应商
- 公司名称: {supplier.name}
- 代码: {supplier.ticker}
- 市场: {supplier.market}
- 市值: {supplier.market_cap} {'亿' if supplier.market == 'a_stock' else 'B'}
- 行业: {supplier.sector}
- 描述: {supplier.description}
- 市盈率: {supplier.pe_ratio}
- 营收增速: {supplier.revenue_growth}
- 毛利率: {supplier.gross_margin}
{financial_block}

请对该供应商进行评估，对以下5个维度各打0-10分:
1. market_position: 市场地位（市占率、垄断/寡头地位）
2. customer_validation: 客户验证（是否有大客户订单、已验证）
3. capacity_status: 产能状况（利用率、扩产计划）
4. financial_health: 财务健康（营收增速、毛利率、现金流）
5. valuation: 估值水平（PE/PB相对行业均值）

同时列出:
- strengths: 优势（2-3条）
- weaknesses: 风险/劣势（1-2条）

返回严格 JSON:
{{
  "market_position": 8,
  "customer_validation": 7,
  "capacity_status": 6,
  "financial_health": 7,
  "valuation": 5,
  "strengths": ["...", "..."],
  "weaknesses": ["..."]
}}"""

        try:
            response = await asyncio.wait_for(
                self.llm.ainvoke(
                    [
                        SystemMessage(content=self._system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                ),
                timeout=120,
            )
            text = response.content.strip()
            text = self._strip_fences(text)
            data = json.loads(text)

            scores = [
                data.get("market_position", 0),
                data.get("customer_validation", 0),
                data.get("capacity_status", 0),
                data.get("financial_health", 0),
                data.get("valuation", 0),
            ]
            overall = sum(scores) / len(scores) if scores else 0

            return SupplierScorecard(
                supplier=supplier,
                bottleneck_node=bottleneck.node_name,
                market_position=data.get("market_position", 0),
                customer_validation=data.get("customer_validation", 0),
                capacity_status=data.get("capacity_status", 0),
                financial_health=data.get("financial_health", 0),
                valuation=data.get("valuation", 0),
                overall_score=round(overall, 1),
                strengths=data.get("strengths", []),
                weaknesses=data.get("weaknesses", []),
                financial_snapshot=financial_snapshot,
            )
        except Exception:
            logger.exception(f"Failed to evaluate supplier: {supplier.name}")
            return SupplierScorecard(
                supplier=supplier,
                bottleneck_node=bottleneck.node_name,
                market_position=0,
                customer_validation=0,
                capacity_status=0,
                financial_health=0,
                valuation=0,
                overall_score=0,
                strengths=[],
                weaknesses=["评估失败"],
                financial_snapshot=financial_snapshot,
            )

    async def evaluate_batch(
        self,
        suppliers: list[SupplierInfo],
        bottleneck: BottleneckReport,
        financial_map: dict[str, FinancialSnapshot] | None = None,
    ) -> list[SupplierScorecard]:
        """Evaluate a batch of suppliers for one bottleneck node."""
        financial_map = financial_map or {}
        tasks = [
            self.evaluate(s, bottleneck, financial_map.get(s.ticker))
            for s in suppliers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scorecards = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Supplier evaluation failed: {r}")
                continue
            scorecards.append(r)

        # Sort by overall score descending
        scorecards.sort(key=lambda sc: sc.overall_score, reverse=True)
        return scorecards

    async def evaluate_all(
        self,
        supplier_map: dict[str, list[SupplierInfo]],
        bottlenecks: list[BottleneckReport],
        financial_map: dict[str, FinancialSnapshot] | None = None,
    ) -> list[SupplierScorecard]:
        """Evaluate suppliers across all bottleneck nodes.

        Args:
            supplier_map: node_name -> list of suppliers
            bottlenecks: bottleneck reports
            financial_map: ticker -> FinancialSnapshot (optional)
        Returns:
            Flat list of all scorecards, sorted by overall_score desc.
        """
        financial_map = financial_map or {}
        all_scorecards: list[SupplierScorecard] = []
        total_suppliers = sum(len(v) for v in supplier_map.values())
        evaluated = 0

        if self._on_progress:
            await self._on_progress(f"── 开始评估 {total_suppliers} 家候选供应商 ──")

        for bn in bottlenecks:
            suppliers = supplier_map.get(bn.node_name, [])
            if not suppliers:
                continue

            if self._on_progress:
                await self._on_progress(f"▸ 评估 {bn.node_name} 的 {len(suppliers)} 家供应商...")

            scorecards = await self.evaluate_batch(suppliers, bn, financial_map)
            all_scorecards.extend(scorecards)
            evaluated += len(scorecards)

            if self._on_progress:
                for sc in scorecards:
                    await self._on_progress(
                        f"✓ {sc.supplier.name}: {sc.overall_score:.1f} 分 "
                        f"({evaluated}/{total_suppliers})"
                    )

            logger.info(
                f"Evaluated {len(scorecards)} suppliers for {bn.node_name}"
            )

        all_scorecards.sort(key=lambda sc: sc.overall_score, reverse=True)

        # 按 ticker（或公司名）去重：保留最高分条目，合并所属瓶颈环节
        deduped: list[SupplierScorecard] = []
        seen: dict[str, int] = {}
        for sc in all_scorecards:
            key = (sc.supplier.ticker or sc.supplier.name).strip()
            if not key:
                deduped.append(sc)
                continue
            if key in seen:
                existing = deduped[seen[key]]
                if sc.bottleneck_node and sc.bottleneck_node not in existing.bottleneck_node:
                    existing.bottleneck_node += f", {sc.bottleneck_node}"
            else:
                seen[key] = len(deduped)
                deduped.append(sc)

        all_scorecards = deduped

        if self._on_progress:
            msg = f"── 供应商评估完成: 共评估 {evaluated} 家"
            if evaluated != len(all_scorecards):
                msg += f", 去重后 {len(all_scorecards)} 家"
            msg += " ──"
            await self._on_progress(msg)

        return all_scorecards

    @staticmethod
    def _strip_fences(text: str) -> str:
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()


# ---------------------------------------------------------------------------
# Alpha / 预期差评分
# ---------------------------------------------------------------------------

class AlphaScorer:
    """计算预期差 (alpha) 评分。

    核心逻辑: alpha = 瓶颈重要性 × (1 - 市场关注度/10)
    市场关注度由研报覆盖量 + 市值规模决定。
    """

    # 市值分档（A 股，亿元）
    CAP_TIERS_YI = [(50, 1), (200, 3), (500, 5), (1000, 7), (float("inf"), 9)]
    # 研报数量分档
    REPORT_TIERS = [(3, 1), (10, 3), (20, 5), (40, 7), (float("inf"), 9)]

    @classmethod
    def _tier_score(cls, value: float | None, tiers: list[tuple]) -> float:
        if value is None:
            return 5.0
        for threshold, score in tiers:
            if value <= threshold:
                return score
        return tiers[-1][1]

    @classmethod
    def compute(
        cls,
        scorecard: SupplierScorecard,
        bottleneck_score: float,
    ) -> AlphaScore:
        """为一张评分卡计算 alpha。"""
        snap = scorecard.financial_snapshot
        cap = scorecard.supplier.market_cap

        cap_attention = cls._tier_score(cap, cls.CAP_TIERS_YI)
        report_attention = 5.0
        if snap and snap.analyst_report_count is not None:
            report_attention = cls._tier_score(snap.analyst_report_count, cls.REPORT_TIERS)

        market_attention = round((cap_attention * 0.4 + report_attention * 0.6), 1)
        market_attention = min(10.0, max(0.0, market_attention))

        information_gap = round(10.0 - market_attention, 1)
        alpha = round(bottleneck_score * (1 - market_attention / 10), 1)
        alpha = min(10.0, max(0.0, alpha))

        parts = []
        if cap is not None:
            parts.append(f"市值{cap}亿")
        if snap and snap.analyst_report_count is not None:
            parts.append(f"研报{snap.analyst_report_count}篇")
        parts.append(f"关注度{market_attention}")
        parts.append(f"瓶颈分{bottleneck_score}")
        reasoning = "，".join(parts) + f" → alpha={alpha}"

        return AlphaScore(
            market_attention=market_attention,
            information_gap=information_gap,
            alpha_score=alpha,
            reasoning=reasoning,
        )

    @classmethod
    def score_all(
        cls,
        scorecards: list[SupplierScorecard],
        bottleneck_map: dict[str, float],
    ) -> list[SupplierScorecard]:
        """为所有评分卡计算 alpha 并挂载。

        Args:
            scorecards: 已完成 LLM 评估的评分卡列表
            bottleneck_map: node_name -> bottleneck overall_score
        Returns:
            同一列表（原地修改）
        """
        for sc in scorecards:
            bn_score = bottleneck_map.get(sc.bottleneck_node.split(",")[0].strip(), 5.0)
            sc.alpha = cls.compute(sc, bn_score)
        return scorecards
