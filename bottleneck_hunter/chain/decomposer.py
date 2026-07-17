"""LLM-driven industry chain decomposer.

Takes an end product (e.g. "GPU") and recursively decomposes the supply chain
N layers deep, producing a ChainGraph.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.json_utils import extract_json_array
from bottleneck_hunter.chain.models import (
    ChainGraph,
    ChainLink,
    IndustryNode,
    LayerType,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _safe_int(val, default: int = 0) -> int:
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return default


def _safe_float(val, default: float = 0.5) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return default


def _normalize_companies(raw: list) -> list[dict]:
    """将 LLM 返回的企业列表统一为 [{name, code}] 格式。"""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, dict):
            result.append({"name": item.get("name", ""), "code": item.get("code", "")})
        elif isinstance(item, str) and item.strip():
            result.append({"name": item.strip(), "code": ""})
    return result


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


# 按拆解层数给整体超时预算（秒）。层数越深、节点扇出越大越慢，故非线性放宽。
# 显式表尊重用户设定：4 层 4200s、5 层 6400s；其余层数按趋势插值/外推。
# BH_DECOMPOSE_TIMEOUT 环境变量存在时**直接覆盖**本表（统一硬上限，便于运维临时调）。
_DECOMPOSE_TIMEOUT_BY_DEPTH = {
    1: 900,     # 15 min
    2: 1800,    # 30 min
    3: 3000,    # 50 min
    4: 4200,    # 70 min（用户指定）
    5: 6400,    # ~107 min（用户指定）
}
_DECOMPOSE_TIMEOUT_PER_EXTRA_LAYER = 2200  # 6 层及以上每多一层再加的预算


def decompose_timeout_for_depth(depth: int) -> int:
    """返回该拆解层数的整体超时预算（秒）。env BH_DECOMPOSE_TIMEOUT 若设则覆盖。"""
    import os
    env = os.getenv("BH_DECOMPOSE_TIMEOUT")
    if env:
        try:
            return max(60, int(env))
        except ValueError:
            pass
    d = max(1, int(depth or 1))
    if d in _DECOMPOSE_TIMEOUT_BY_DEPTH:
        return _DECOMPOSE_TIMEOUT_BY_DEPTH[d]
    # 超出显式表（≥6 层）：从最深已知档位按每层增量外推
    top = max(_DECOMPOSE_TIMEOUT_BY_DEPTH)
    return _DECOMPOSE_TIMEOUT_BY_DEPTH[top] + (d - top) * _DECOMPOSE_TIMEOUT_PER_EXTRA_LAYER


class ChainDecomposer:
    """Decomposes an industry chain from an end product using LLM."""

    LLM_TIMEOUT = 120  # 单次 LLM 调用超时（秒）
    MAX_CONCURRENCY = 4  # 同层并发数量上限
    MAX_RETRIES = 2  # LLM 调用失败重试次数
    # 广度上限：防止逐层指数爆炸（曾单层 283 节点 → 整体 >30min 超时）。
    # 调优记录：24/6 过紧→偏少；40/10 健康(人形机器人 depth4 用~70次调用/9min/88节点)。
    # 用户要「质量优先」再放宽：60/12。LLM 调用数只由每层展开父节点数决定，仍在 1800s 预算内
    # (预算约容 360 次)；注意节点变多会拉长下游瓶颈打分(约 10s/环节)，属预期取舍。
    MAX_NODES_PER_LAYER = 60
    MAX_CHILDREN_PER_NODE = 12

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
        # 统计计数器（每次 decompose 调用前重置）
        self._timeout_count = 0
        self._retry_count = 0
        self._last_fail_reason = ""  # 最近一次 LLM 调用失败的中文原因(供上层弹窗判定主模型失败)

    async def decompose(self, end_product: str, on_layer_start=None, on_progress=None,
                         deadline: float | None = None) -> ChainGraph:
        """Run full decomposition and return a ChainGraph.

        deadline: asyncio 事件循环时钟(loop.time())的绝对截止时刻。层间检查剩余时间，
            不足以再钻一层时**优雅停止**并返回已拆好的图(metadata['partial']=True)，
            而非被外层硬取消丢弃全部已完成层。None 则不限时(由外层兜底)。
        """
        # 重置统计计数器
        self._timeout_count = 0
        self._retry_count = 0
        self._last_fail_reason = ""
        self._on_progress = on_progress

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

        # 失败统计
        fail_count = 0
        timeout_count = 0
        retry_count = 0

        # Iteratively decompose each layer
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENCY)
        _last_layer_sec = 0.0  # 上一层实际耗时，用于自适应预估下一层（比最坏假设宽松）

        for depth in range(1, self.max_depth + 1):
            _layer_t0 = asyncio.get_event_loop().time()
            # 层间 deadline 检查：只在「剩余预算 < 下一层预估耗时」时才优雅停（保住已完成层）。
            # 预估用**上一层实际耗时 ×1.5 安全系数**，而非最坏假设——避免明明预算够却提前停，
            # 契合「限制更少、允许跑更久」。无历史(理论上 depth>1 必有)时退回一个温和估计。
            if deadline is not None and depth > 1:
                est_layer_sec = (_last_layer_sec * 1.5) if _last_layer_sec > 0 else (self.LLM_TIMEOUT * 3)
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining < est_layer_sec:
                    logger.warning("拆解 deadline 将至(剩 %.0fs<下一层预估 %.0fs)，停在第 %d 层，返回部分结果",
                                   remaining, est_layer_sec, depth - 1)
                    if on_progress:
                        await on_progress(f"⏱ 时间预算将尽，停在第 {depth - 1} 层，返回已拆解部分（可减少层数/换更快模型重试）")
                    graph.metadata["partial"] = True
                    graph.metadata["stopped_at_layer"] = depth - 1
                    break
            parent_nodes = graph.get_nodes_at_layer(depth - 1)
            # 广度上限：父节点过多时只展开最关键的前 N 个（按其对下游的 dependency 排序），
            # 其余节点仍保留在图中作为叶子，只是不再向上钻取——避免指数爆炸导致整体超时。
            if len(parent_nodes) > self.MAX_NODES_PER_LAYER:
                skipped = len(parent_nodes) - self.MAX_NODES_PER_LAYER
                parent_nodes = self._top_parents(graph, parent_nodes, self.MAX_NODES_PER_LAYER)
                logger.info(f"层 {depth} 父节点 {len(parent_nodes) + skipped} 个，按重要度只展开前 {self.MAX_NODES_PER_LAYER} 个")
                if on_progress:
                    await on_progress(f"⚠ 第 {depth} 层节点过多，按重要度只展开前 {self.MAX_NODES_PER_LAYER} 个（略过 {skipped} 个）")
            if on_layer_start:
                await on_layer_start(depth, self.max_depth, len(parent_nodes))

            existing_names = [n.name for n in graph.nodes]

            async def _process_parent(parent, existing, _depth=depth):
                async with semaphore:
                    if on_progress:
                        await on_progress(f"▸ 拆解: {parent.name} (层 {_depth})")
                    children = await self._decompose_layer(end_product, parent.name, _depth, existing)
                    if on_progress:
                        await on_progress(f"✓ {parent.name} → {len(children)} 个子节点")
                    return parent, children

            tasks = [_process_parent(p, existing_names[:]) for p in parent_nodes]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"层 {depth} 拆解失败: {result}")
                    fail_count += 1
                    if on_progress:
                        await on_progress(f"✗ 层 {depth} 异常: {result}")
                    continue
                parent, children = result
                # 每个父节点只保留 K 个最关键子节点（按 dependency 降序），控制节点总量
                children = sorted(
                    children,
                    key=lambda c: _safe_float(c.get("dependency", 0.5), 0.5),
                    reverse=True,
                )[:self.MAX_CHILDREN_PER_NODE]
                for child_data in children:
                    child_name = child_data["name"]

                    merged_name = self._find_similar_node(child_name, existing_names)
                    if merged_name:
                        logger.info(f"合并相似节点: '{child_name}' → '{merged_name}'")
                        child_name = merged_name

                    if not graph.get_node(child_name):
                        raw_companies = child_data.get("representative_companies", [])
                        companies = _normalize_companies(raw_companies)
                        child = IndustryNode(
                            name=child_name,
                            description=child_data.get("description", ""),
                            layer=depth,
                            layer_type=_layer_type_for_depth(depth),
                            function=child_data.get("function", ""),
                            key_parameters=child_data.get("key_parameters", []),
                            upstream_deps=child_data.get("upstream_deps", []),
                            downstream_deps=[parent.name],
                            representative_companies=companies,
                        )
                        graph.nodes.append(child)
                        existing_names.append(child_name)

                    link = ChainLink(
                        upstream=child_name,
                        downstream=parent.name,
                        dependency=_safe_float(child_data.get("dependency", 0.5), 0.5),
                        alternatives=_safe_int(child_data.get("alternatives", 0)),
                        notes=str(child_data.get("notes", "")),
                    )
                    graph.links.append(link)

            node_count = len(graph.get_nodes_at_layer(depth))
            logger.info(f"Decomposed layer {depth}: found {node_count} nodes")
            _last_layer_sec = asyncio.get_event_loop().time() - _layer_t0  # 记录本层实际耗时供下层自适应预估
            if on_progress:
                await on_progress(f"── 层 {depth} 完成: {node_count} 个新节点，累计 {len(graph.nodes)} 个 ──")

        # 汇总 _decompose_layer 中的重试和超时统计
        timeout_count = self._timeout_count
        retry_count = self._retry_count

        # 全局语义去重
        if on_progress:
            await on_progress("▸ 语义去重合并中...")
        graph = await self._merge_similar_nodes(graph)
        # 去重后各层节点数 + 总计，ASCII 标记便于 Loki 检索(|= "decompose done")定位产业数量
        _layer_nodes = {L: len(graph.get_nodes_at_layer(L)) for L in range(self.max_depth + 1)}
        logger.info("decompose done: product=%s depth=%d caps=%d/%d layer_nodes=%s total=%d",
                    end_product, self.max_depth, self.MAX_NODES_PER_LAYER, self.MAX_CHILDREN_PER_NODE,
                    _layer_nodes, len(graph.nodes))
        if on_progress:
            await on_progress(f"✓ 去重完成，最终 {len(graph.nodes)} 个节点")

        # 将失败统计写入 metadata
        graph.metadata["llm_failures"] = fail_count
        graph.metadata["llm_timeouts"] = timeout_count
        graph.metadata["llm_retries"] = retry_count
        graph.metadata["total_failures"] = fail_count + timeout_count

        if fail_count + timeout_count > 0:
            logger.warning(
                f"拆解完成，共 {fail_count + timeout_count} 次失败"
                f"（节点异常 {fail_count} 次，超时放弃 {timeout_count} 次，重试 {retry_count} 次）"
            )

        # 自动保存产业链版本——但**只保存有效链**：若第 1 层就没拆出任何子节点(LLM 全失败/
        # 额度不足等)，得到的是 root-only 退化链，保存它会污染缓存(后续被复用成"1 层供应链")。
        # 有效判据：至少拆出 1 层子节点(总节点 > 1 且存在 layer≥1 的节点)。
        _has_children = len(graph.nodes) > 1 and any((n.layer or 0) >= 1 for n in graph.nodes)
        if not _has_children:
            logger.warning("拆解未产出任何子节点(第1层全失败?)，不保存退化链，交由上层报错重试")
            graph.metadata["decompose_failed"] = True
            if self._last_fail_reason:
                graph.metadata["fail_reason"] = self._last_fail_reason  # 供上层判定主模型失败原因
            self._on_progress = None
            return graph

        model_name = getattr(self.llm, "model_name", "") or getattr(self.llm, "model", "") or ""
        graph.model_used = str(model_name)
        try:
            from datetime import datetime, timezone
            graph.created_at = datetime.now(timezone.utc).isoformat()
            from bottleneck_hunter.chain.chain_store import ChainStore
            store = ChainStore()
            store.save_chain(end_product, graph.model_dump(), model_used=str(model_name))
        except Exception:
            logger.exception("产业链版本保存失败，不影响拆解结果")

        self._on_progress = None
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
- representative_companies: 该环节最具代表性的上市公司列表（2-4家），每个元素包含 name（公司简称）和 code（股票代码）
  - 优先推荐A股上市公司，code 格式为6位数字（如"002371"，沪市6开头、深市0或3开头、北交所4或8开头）
  - 若该环节主要为美股上市公司，code 为字母ticker（如"NVDA"）
  - 若该环节无上市公司或不确定，code 留空字符串
  - 务必确保推荐的公司确实在该环节有核心业务，不要为了凑数而推荐不相关的公司

只返回 JSON 数组，不要其他文字。"""

        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content=user_prompt),
        ]

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    self.llm.ainvoke(messages),
                    timeout=self.LLM_TIMEOUT,
                )
                break
            except asyncio.TimeoutError:
                if attempt < self.MAX_RETRIES:
                    self._retry_count += 1
                    logger.warning(f"LLM 超时 ({self.LLM_TIMEOUT}s)，重试 {attempt + 1}/{self.MAX_RETRIES}: {parent_name}")
                    if self._on_progress:
                        await self._on_progress(f"⚠ 超时重试 {attempt + 1}/{self.MAX_RETRIES}: {parent_name}")
                    await asyncio.sleep(2)
                else:
                    self._timeout_count += 1
                    logger.error(f"LLM 调用超时，已放弃: {parent_name} (层 {depth})")
                    self._last_fail_reason = "请求超时"
                    if self._on_progress:
                        await self._on_progress(f"✗ 超时放弃: {parent_name}")
                    return []
            except Exception as e:
                if attempt < self.MAX_RETRIES:
                    self._retry_count += 1
                    logger.warning(f"LLM 调用失败，重试 {attempt + 1}/{self.MAX_RETRIES}: {parent_name} - {e}")
                    if self._on_progress:
                        await self._on_progress(f"⚠ 失败重试 {attempt + 1}/{self.MAX_RETRIES}: {parent_name}")
                    await asyncio.sleep(2)
                else:
                    self._timeout_count += 1
                    logger.error(f"LLM 调用失败，已放弃: {parent_name} (层 {depth}) - {e}")
                    try:
                        from bottleneck_hunter.llm_clients.fallback import classify_reason
                        self._last_fail_reason = classify_reason(e)
                    except Exception:  # noqa: BLE001
                        self._last_fail_reason = "调用异常"
                    if self._on_progress:
                        await self._on_progress(f"✗ 调用失败: {parent_name}")
                    return []

        return extract_json_array(response.content) or []

    # ── 本地规则去重 ─────────────────────────────────────

    @staticmethod
    def _top_parents(graph: ChainGraph, parents: list, limit: int) -> list:
        """按节点重要度取前 limit 个父节点展开。

        重要度 = 该节点作为上游时、对下游链接的最大 dependency（越关键越优先钻取）。
        """
        importance: dict[str, float] = {}
        for link in graph.links:
            if link.dependency > importance.get(link.upstream, -1.0):
                importance[link.upstream] = link.dependency
        return sorted(parents, key=lambda n: importance.get(n.name, 0.5), reverse=True)[:limit]

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
            response = await asyncio.wait_for(
                self.llm.ainvoke(
                    [SystemMessage(content="你是产业链分析专家，请精确识别语义重复的环节。"),
                     HumanMessage(content=prompt)]
                ),
                timeout=self.LLM_TIMEOUT,
            )
            merge_groups = extract_json_array(response.content)
            if not merge_groups:
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
