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

    async def decompose(self, end_product: str, on_layer_start=None) -> ChainGraph:
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
            if on_layer_start:
                await on_layer_start(depth, self.max_depth, len(parent_nodes))

            existing_names = [n.name for n in graph.nodes]

            for parent in parent_nodes:
                children = await self._decompose_layer(end_product, parent.name, depth, existing_names)
                for child_data in children:
                    child_name = child_data["name"]

                    # 语义去重：检查是否与已有节点高度相似
                    merged_name = self._find_similar_node(child_name, existing_names)
                    if merged_name:
                        logger.info(f"合并相似节点: '{child_name}' → '{merged_name}'")
                        child_name = merged_name

                    if not graph.get_node(child_name):
                        child = IndustryNode(
                            name=child_name,
                            description=child_data.get("description", ""),
                            layer=depth,
                            layer_type=_layer_type_for_depth(depth),
                            function=child_data.get("function", ""),
                            key_parameters=child_data.get("key_parameters", []),
                            upstream_deps=child_data.get("upstream_deps", []),
                            downstream_deps=[parent.name],
                        )
                        graph.nodes.append(child)
                        existing_names.append(child_name)

                    link = ChainLink(
                        upstream=child_name,
                        downstream=parent.name,
                        dependency=child_data.get("dependency", 0.5),
                        alternatives=child_data.get("alternatives", 0),
                        notes=child_data.get("notes", ""),
                    )
                    graph.links.append(link)

            logger.info(f"Decomposed layer {depth}: found {len(graph.get_nodes_at_layer(depth))} nodes")

        # 全局语义去重
        graph = await self._merge_similar_nodes(graph)

        return graph

    async def _decompose_layer(
        self, end_product: str, parent_name: str, depth: int, existing_names: list[str] | None = None
    ) -> list[dict]:
        """Ask LLM to decompose one node into its upstream components."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        existing_hint = ""
        if existing_names and len(existing_names) > 1:
            names_str = "、".join(existing_names[:60])  # 防止过长
            existing_hint = f"\n已有环节（请勿重复或输出语义相似的名称）: {names_str}\n"

        user_prompt = f"""{lang_note}

终端产品: {end_product}
当前分析: {parent_name} (第{depth}层)
{existing_hint}
请拆解 {parent_name} 的上游关键零部件、原材料或设备。
注意：不要输出与「已有环节」中语义重复或高度相似的名称。如果某个上游环节已存在（即使名称略有不同），请直接使用已有名称。

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

    # ── 本地规则去重 ─────────────────────────────────────

    @staticmethod
    def _find_similar_node(new_name: str, existing_names: list[str]) -> str | None:
        """基于简单规则检测语义重复。

        返回已存在的节点名（应合并到该名称），或 None（无重复）。
        """
        # 预处理：去除常见修饰词后的核心词
        def _core(name: str) -> str:
            import re
            # 去掉括号内容
            name = re.sub(r"[（(][^)）]*[）)]", "", name)
            # 去掉常见前缀修饰
            for prefix in ("高端", "先进", "精密", "超高纯", "高纯", "高性能",
                           "新型", "专用", "关键", "核心", "特种"):
                name = name.removeprefix(prefix)
            # 去掉常见后缀修饰
            for suffix in ("材料", "组件", "模组", "系统", "设备", "装置"):
                if len(name) > len(suffix) + 1:
                    name = name.removesuffix(suffix)
            return name.strip()

        new_core = _core(new_name)
        if len(new_core) < 2:
            return None

        for existing in existing_names:
            if existing == new_name:
                continue
            existing_core = _core(existing)
            if len(existing_core) < 2:
                continue

            # 规则1: 核心词完全相同
            if new_core == existing_core:
                return existing

            # 规则2: 一方包含另一方的核心词（且核心词≥2字）
            if len(new_core) >= 2 and len(existing_core) >= 2:
                if new_core in existing_core or existing_core in new_core:
                    # 保留更短的名称（更通用）
                    return existing

            # 规则3: "X及Y" 和 "X" 视为同一环节
            if "及" in new_name or "和" in new_name or "与" in new_name:
                parts = new_name.replace("和", "及").replace("与", "及").split("及")
                for part in parts:
                    part = part.strip()
                    if part and (_core(part) == existing_core or part == existing):
                        return existing

        return None

    # ── LLM 全局语义合并 ────────────────────────────────

    async def _merge_similar_nodes(self, graph: ChainGraph) -> ChainGraph:
        """拆解完成后，用 LLM 识别语义重复的节点并合并。"""
        node_names = [n.name for n in graph.nodes if n.layer > 0]
        if len(node_names) < 3:
            return graph

        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"
        prompt = f"""{lang_note}

以下是一条产业链中拆解出来的所有环节名称：
{json.dumps(node_names, ensure_ascii=False)}

请识别其中**语义重复或高度相似**的名称组（指向同一类产品/技术/材料的不同叫法）。

判断标准：
- 核心供应商群体高度重叠（>70%）
- 仅是措辞/修饰不同（如"高端光刻机"和"先进光刻机"）
- 仅是粒度不同（如"光刻设备及光刻胶"应拆分而非合并为一个；但"光刻设备"和"光刻机"是同一类）

对每组重复名称，选出一个最具代表性的保留名称。

返回严格 JSON 数组，每个元素为：
{{"keep": "保留的名称", "merge": ["要合并到keep的名称1", "要合并到keep的名称2"]}}

如果没有任何重复，返回空数组 []。
只返回 JSON 数组，不要其他文字。"""

        try:
            response = await self.llm.ainvoke(
                [SystemMessage(content="你是产业链分析专家，请精确识别语义重复的环节。"),
                 HumanMessage(content=prompt)]
            )
            text = response.content.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:])
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            merge_groups = json.loads(text)
            if not isinstance(merge_groups, list) or not merge_groups:
                return graph

            # 构建合并映射: old_name → keep_name
            rename_map: dict[str, str] = {}
            for group in merge_groups:
                keep = group.get("keep", "")
                merges = group.get("merge", [])
                if not keep or not merges:
                    continue
                # 确保 keep 名称确实存在
                if keep not in node_names:
                    # 如果 keep 名称不在节点中，从 merge 列表中找一个存在的
                    found = False
                    for m in merges:
                        if m in node_names:
                            keep, _ = m, merges
                            found = True
                            break
                    if not found:
                        continue

                for old_name in merges:
                    if old_name != keep and old_name in node_names:
                        rename_map[old_name] = keep

            if not rename_map:
                return graph

            logger.info(f"LLM 语义合并: {rename_map}")

            # 执行合并
            graph = self._apply_merge(graph, rename_map)

        except Exception:
            logger.exception("LLM 语义合并失败，跳过合并步骤")

        return graph

    @staticmethod
    def _apply_merge(graph: ChainGraph, rename_map: dict[str, str]) -> ChainGraph:
        """将 rename_map 中的旧节点名合并到目标节点。"""
        # 1. 删除被合并的节点
        kept_nodes = [n for n in graph.nodes if n.name not in rename_map]
        graph.nodes = kept_nodes

        # 2. 更新所有链接中的引用
        new_links: list[ChainLink] = []
        seen_links: set[tuple[str, str]] = set()

        for link in graph.links:
            up = rename_map.get(link.upstream, link.upstream)
            down = rename_map.get(link.downstream, link.downstream)
            # 跳过自环
            if up == down:
                continue
            # 跳过重复链接（保留依赖度更高的）
            key = (up, down)
            if key in seen_links:
                continue
            seen_links.add(key)
            new_links.append(ChainLink(
                upstream=up,
                downstream=down,
                dependency=link.dependency,
                alternatives=link.alternatives,
                notes=link.notes,
            ))

        graph.links = new_links

        # 3. 更新节点的依赖引用
        for node in graph.nodes:
            node.upstream_deps = [
                rename_map.get(dep, dep) for dep in node.upstream_deps
            ]
            node.downstream_deps = [
                rename_map.get(dep, dep) for dep in node.downstream_deps
            ]
            # 去重
            node.upstream_deps = list(dict.fromkeys(node.upstream_deps))
            node.downstream_deps = list(dict.fromkeys(node.downstream_deps))

        return graph
