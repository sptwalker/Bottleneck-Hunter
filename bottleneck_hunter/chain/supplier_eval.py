"""LLM-driven supplier evaluation.

Takes candidate SupplierInfo from the search step and evaluates each
against a bottleneck node, producing a SupplierScorecard.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    BottleneckReport,
    SupplierInfo,
    SupplierScorecard,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


class SupplierEvaluator:
    """Evaluate candidate suppliers using LLM."""

    def __init__(
        self,
        llm: BaseChatModel,
        language: str = "zh",
    ):
        self.llm = llm
        self.language = language
        self._system_prompt = _load_prompt("supplier_eval")

    async def evaluate(
        self,
        supplier: SupplierInfo,
        bottleneck: BottleneckReport,
    ) -> SupplierScorecard:
        """Evaluate a single supplier against a bottleneck node."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

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
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(content=user_prompt),
                ]
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
            )

    async def evaluate_batch(
        self,
        suppliers: list[SupplierInfo],
        bottleneck: BottleneckReport,
    ) -> list[SupplierScorecard]:
        """Evaluate a batch of suppliers for one bottleneck node."""
        tasks = [self.evaluate(s, bottleneck) for s in suppliers]
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
    ) -> list[SupplierScorecard]:
        """Evaluate suppliers across all bottleneck nodes.

        Args:
            supplier_map: node_name -> list of suppliers
            bottlenecks: bottleneck reports
        Returns:
            Flat list of all scorecards, sorted by overall_score desc.
        """
        all_scorecards: list[SupplierScorecard] = []

        for bn in bottlenecks:
            suppliers = supplier_map.get(bn.node_name, [])
            if not suppliers:
                continue

            scorecards = await self.evaluate_batch(suppliers, bn)
            all_scorecards.extend(scorecards)
            logger.info(
                f"Evaluated {len(scorecards)} suppliers for {bn.node_name}"
            )

        all_scorecards.sort(key=lambda sc: sc.overall_score, reverse=True)
        return all_scorecards

    @staticmethod
    def _strip_fences(text: str) -> str:
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
        return text.strip()


import asyncio  # noqa: E402 — needed for gather
