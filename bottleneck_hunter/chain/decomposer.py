"""LLM-driven industry chain decomposer.

Takes an end product (e.g. "GPU") and recursively decomposes the supply chain
N layers deep, producing a ChainGraph.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    ChainGraph,
    ChainLink,
    IndustryNode,
    LayerType,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

LAYER_TYPE_MAP = {
    0: LayerType.END_PRODUCT,
    1: LayerType.ASSEMBLY,
    2: LayerType.COMPONENT,
    3: LayerType.SUB_COMPONENT,
    4: LayerType.MATERIAL,
    5: LayerType.RAW_MATERIAL,
}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


def _layer_type_for_depth(depth: int) -> LayerType:
    return LAYER_TYPE_MAP.get(depth, LayerType.EQUIPMENT)


class ChainDecomposer:
    """Decomposes an industry chain from an end product using LLM."""

    def __init__(
        self,
        llm: BaseChatModel,
        max_depth: int = 3,
        sector: str = "",
        language: str = "zh",
    ):
        self.llm = llm
        self.max_depth = max_depth
        self.sector = sector
        self.language = language
        self._system_prompt = _load_prompt("decompose")

    async def decompose(self, end_product: str) -> ChainGraph:
        """Run full decomposition and return a ChainGraph."""
        graph = ChainGraph(
            sector=self.sector or end_product,
            end_product=end_product,
            max_depth=self.max_depth,
        )

        # Add the root node
        root = IndustryNode(
            name=end_product,
            description=f"{end_product} 终端产品" if self.language == "zh" else f"{end_product} end product",
            layer=0,
            layer_type=LayerType.END_PRODUCT,
            function="Final product",
        )
        graph.nodes.append(root)

        # Iteratively decompose each layer
        for depth in range(1, self.max_depth + 1):
            parent_nodes = graph.get_nodes_at_layer(depth - 1)
            for parent in parent_nodes:
                children = await self._decompose_layer(end_product, parent.name, depth)
                for child_data in children:
                    child = IndustryNode(
                        name=child_data["name"],
                        description=child_data.get("description", ""),
                        layer=depth,
                        layer_type=_layer_type_for_depth(depth),
                        function=child_data.get("function", ""),
                        key_parameters=child_data.get("key_parameters", []),
                        upstream_deps=child_data.get("upstream_deps", []),
                        downstream_deps=[parent.name],
                    )
                    if not graph.get_node(child.name):
                        graph.nodes.append(child)

                    link = ChainLink(
                        upstream=child.name,
                        downstream=parent.name,
                        dependency=child_data.get("dependency", 0.5),
                        alternatives=child_data.get("alternatives", 0),
                        notes=child_data.get("notes", ""),
                    )
                    graph.links.append(link)

            logger.info(f"Decomposed layer {depth}: found {len(graph.get_nodes_at_layer(depth))} nodes")

        return graph

    async def _decompose_layer(
        self, end_product: str, parent_name: str, depth: int
    ) -> list[dict]:
        """Ask LLM to decompose one node into its upstream components."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        user_prompt = f"""{lang_note}

终端产品: {end_product}
当前分析: {parent_name} (第{depth}层)
请拆解 {parent_name} 的上游关键零部件、原材料或设备。

返回严格的 JSON 数组，每个元素包含:
- name: 上游环节名称
- description: 简要描述
- function: 在产业链中的功能
- key_parameters: 关键技术参数列表
- upstream_deps: 该环节的上游依赖（名称列表）
- dependency: 对下游的重要程度 0-1
- alternatives: 已知替代方案数量
- notes: 补充说明

只返回 JSON 数组，不要其他文字。"""

        response = await self.llm.ainvoke(
            [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_prompt),
            ]
        )

        text = response.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"LLM returned invalid JSON for {parent_name} layer {depth}, attempting extraction")
            return self._extract_json_from_text(text)

    @staticmethod
    def _extract_json_from_text(text: str) -> list[dict]:
        """Fallback: try to find a JSON array anywhere in the text."""
        import re

        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.error("Could not extract JSON from LLM response")
        return []
