"""Bottleneck identification and scoring.

Evaluates each node in a ChainGraph for bottleneck characteristics:
scarcity, irreplaceability, supply-demand gap, pricing power, tech barrier.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Default weights for overall score calculation
DEFAULT_WEIGHTS: dict[BottleneckDimension, float] = {
    BottleneckDimension.SCARCITY: 0.25,
    BottleneckDimension.IRREPLACEABILITY: 0.25,
    BottleneckDimension.SUPPLY_DEMAND_GAP: 0.20,
    BottleneckDimension.PRICING_POWER: 0.15,
    BottleneckDimension.TECH_BARRIER: 0.15,
}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


DIMENSION_DESC = {
    BottleneckDimension.SCARCITY: "供应商数量稀少、市场集中度高",
    BottleneckDimension.IRREPLACEABILITY: "是否存在替代技术或材料",
    BottleneckDimension.SUPPLY_DEMAND_GAP: "当前及未来供需缺口大小",
    BottleneckDimension.PRICING_POWER: "该环节的涨价能力和定价权",
    BottleneckDimension.TECH_BARRIER: "技术壁垒、专利保护、认证周期",
}


class BottleneckAnalyzer:
    """Analyzes chain nodes for bottleneck characteristics."""

    def __init__(
        self,
        llm: BaseChatModel,
        weights: dict[BottleneckDimension, float] | None = None,
        language: str = "zh",
    ):
        self.llm = llm
        self.weights = weights or DEFAULT_WEIGHTS
        self.language = language
        self._system_prompt = _load_prompt("bottleneck")

    async def analyze(self, graph: ChainGraph, top_n: int = 5) -> list[BottleneckReport]:
        """Analyze all non-root nodes and return ranked bottleneck reports."""
        # Skip layer 0 (end product itself)
        candidates = [n for n in graph.nodes if n.layer > 0]
        reports: list[BottleneckReport] = []

        for node in candidates:
            report = await self._analyze_node(node.name, node.description, node.layer, graph)
            if report:
                reports.append(report)

        # Rank by overall score descending
        reports.sort(key=lambda r: r.overall_score, reverse=True)
        for i, r in enumerate(reports):
            r.rank = i + 1

        return reports[:top_n]

    async def _analyze_node(
        self, node_name: str, description: str, layer: int, graph: ChainGraph
    ) -> BottleneckReport | None:
        """Score a single node across all bottleneck dimensions."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        chain_context = self._build_context(node_name, graph)

        user_prompt = f"""{lang_note}

产业链: {graph.sector}
分析环节: {node_name}
层级: 第{layer}层
描述: {description}

{chain_context}

请对该环节进行瓶颈分析，对以下5个维度各打0-10分，并给出理由:
{chr(10).join(f"- {d.value}: {desc}" for d, desc in DIMENSION_DESC.items())}

同时列出:
- key_insights: 关键洞察（2-3条）
- risks: 主要风险（1-2条）

返回严格 JSON:
{{
  "scores": [
    {{"dimension": "scarcity", "score": 8, "reasoning": "..."}},
    {{"dimension": "irreplaceability", "score": 9, "reasoning": "..."}},
    {{"dimension": "supply_demand_gap", "score": 7, "reasoning": "..."}},
    {{"dimension": "pricing_power", "score": 6, "reasoning": "..."}},
    {{"dimension": "tech_barrier", "score": 8, "reasoning": "..."}}
  ],
  "key_insights": ["...", "..."],
  "risks": ["...", "..."]
}}"""

        try:
            response = await self.llm.ainvoke(
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(content=user_prompt),
                ]
            )
            text = response.content.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:])
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            data = json.loads(text)

            scores = [
                BottleneckScore(
                    dimension=s["dimension"],
                    score=s["score"],
                    reasoning=s["reasoning"],
                )
                for s in data["scores"]
            ]

            overall = self._weighted_score(scores)

            return BottleneckReport(
                node_name=node_name,
                node_description=description,
                layer=layer,
                scores=scores,
                overall_score=overall,
                key_insights=data.get("key_insights", []),
                risks=data.get("risks", []),
            )
        except Exception:
            logger.exception(f"Failed to analyze node: {node_name}")
            return None

    def _weighted_score(self, scores: list[BottleneckScore]) -> float:
        score_map = {s.dimension: s.score for s in scores}
        total_weight = sum(self.weights.values())
        return sum(
            score_map.get(dim.value, 0) * weight
            for dim, weight in self.weights.items()
        ) / total_weight if total_weight else 0

    @staticmethod
    def _build_context(node_name: str, graph: ChainGraph) -> str:
        """Build context string showing the node's position in the chain."""
        upstream = graph.get_upstream(node_name)
        downstream = graph.get_downstream(node_name)
        lines = [f"当前环节: {node_name}"]
        if downstream:
            lines.append(f"下游环节: {', '.join(n.name for n in downstream)}")
        if upstream:
            lines.append(f"上游环节: {', '.join(n.name for n in upstream)}")
        return "\n".join(lines)
