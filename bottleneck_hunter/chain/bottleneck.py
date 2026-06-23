"""Bottleneck identification and scoring.

Evaluates each node in a ChainGraph for bottleneck characteristics:
scarcity, irreplaceability, supply-demand gap, pricing power, tech barrier.
"""

from __future__ import annotations

import asyncio
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

    LLM_TIMEOUT = 120
    MAX_CONCURRENCY = 4
    MAX_RETRIES = 2

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
        self._timeout_count = 0
        self._retry_count = 0
        self._failed_nodes: list[dict] = []

    @property
    def failed_nodes(self) -> list[dict]:
        return list(self._failed_nodes)

    async def analyze(self, graph: ChainGraph, top_n: int = 5, on_progress=None) -> list[BottleneckReport]:
        """Analyze all non-root nodes and return ranked bottleneck reports."""
        self._timeout_count = 0
        self._retry_count = 0
        self._failed_nodes = []
        self._on_progress = on_progress

        candidates = [n for n in graph.nodes if n.layer > 0]
        total = len(candidates)
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENCY)

        async def _task(node, idx):
            async with semaphore:
                if on_progress:
                    await on_progress(f"▸ 分析: {node.name} ({idx + 1}/{total})")
                result = await self._analyze_node(node.name, node.description, node.layer, graph)
                if result and on_progress:
                    await on_progress(f"✓ {node.name}: {result.overall_score:.1f} 分 ({idx + 1}/{total})")
                elif not result:
                    self._failed_nodes.append({
                        "name": node.name,
                        "description": node.description,
                        "layer": node.layer,
                    })
                    if on_progress:
                        await on_progress(f"✗ {node.name}: 分析失败 ({idx + 1}/{total})")
                return result

        results = await asyncio.gather(
            *[_task(n, i) for i, n in enumerate(candidates)], return_exceptions=True
        )

        reports: list[BottleneckReport] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"瓶颈分析异常: {r}")
                node = candidates[i]
                self._failed_nodes.append({
                    "name": node.name,
                    "description": node.description,
                    "layer": node.layer,
                })
                continue
            if r is not None:
                reports.append(r)

        reports.sort(key=lambda r: r.overall_score, reverse=True)
        for i, rpt in enumerate(reports):
            rpt.rank = i + 1

        if self._timeout_count > 0:
            logger.warning(f"瓶颈分析: {self._timeout_count} 次超时放弃, {self._retry_count} 次重试")

        self._on_progress = None
        return reports

    async def retry_failed_nodes(
        self, graph: ChainGraph, on_progress=None,
    ) -> list[BottleneckReport]:
        """Retry only the previously failed nodes. Returns successful reports."""
        if not self._failed_nodes:
            return []

        nodes_to_retry = list(self._failed_nodes)
        self._failed_nodes = []
        self._timeout_count = 0
        self._retry_count = 0
        self._on_progress = on_progress

        total = len(nodes_to_retry)
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENCY)

        async def _task(node_info, idx):
            async with semaphore:
                name = node_info["name"]
                if on_progress:
                    await on_progress(f"▸ 补充分析: {name} ({idx + 1}/{total})")
                result = await self._analyze_node(
                    name, node_info["description"], node_info["layer"], graph,
                )
                if result and on_progress:
                    await on_progress(f"✓ {name}: {result.overall_score:.1f} 分 ({idx + 1}/{total})")
                elif not result:
                    self._failed_nodes.append(node_info)
                    if on_progress:
                        await on_progress(f"✗ {name}: 补充分析失败 ({idx + 1}/{total})")
                return result

        results = await asyncio.gather(
            *[_task(n, i) for i, n in enumerate(nodes_to_retry)],
            return_exceptions=True,
        )

        reports: list[BottleneckReport] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"补充分析异常: {r}")
                self._failed_nodes.append(nodes_to_retry[i])
                continue
            if r is not None:
                reports.append(r)

        self._on_progress = None
        return reports

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
            messages = [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_prompt),
            ]
            response = None
            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    response = await asyncio.wait_for(
                        self.llm.ainvoke(messages), timeout=self.LLM_TIMEOUT,
                    )
                    break
                except asyncio.TimeoutError:
                    if attempt < self.MAX_RETRIES:
                        self._retry_count += 1
                        logger.warning(f"瓶颈分析超时，重试 {attempt + 1}/{self.MAX_RETRIES}: {node_name}")
                        if self._on_progress:
                            await self._on_progress(f"⚠ 超时重试 {attempt + 1}/{self.MAX_RETRIES}: {node_name}")
                        await asyncio.sleep(2)
                    else:
                        self._timeout_count += 1
                        logger.error(f"瓶颈分析超时，已放弃: {node_name}")
                        if self._on_progress:
                            await self._on_progress(f"✗ 超时放弃: {node_name}")
                        return None
                except Exception as e:
                    if attempt < self.MAX_RETRIES:
                        self._retry_count += 1
                        logger.warning(f"瓶颈分析失败，重试 {attempt + 1}/{self.MAX_RETRIES}: {node_name} - {e}")
                        if self._on_progress:
                            await self._on_progress(f"⚠ 失败重试 {attempt + 1}/{self.MAX_RETRIES}: {node_name}")
                        await asyncio.sleep(2)
                    else:
                        self._timeout_count += 1
                        logger.error(f"瓶颈分析失败，已放弃: {node_name} - {e}")
                        if self._on_progress:
                            await self._on_progress(f"✗ 调用失败: {node_name}")
                        return None

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
