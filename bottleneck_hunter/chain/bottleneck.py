"""Bottleneck identification and scoring.

Evaluates each node in a ChainGraph for bottleneck characteristics:
scarcity, irreplaceability, supply-demand gap, pricing power, tech barrier.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
)
from bottleneck_hunter.chain.json_utils import extract_json_object

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

# 分行业瓶颈评分权重 —— 不同行业的瓶颈特征侧重不同
INDUSTRY_WEIGHTS: dict[str, dict[BottleneckDimension, float]] = {
    "半导体": {
        BottleneckDimension.SCARCITY: 0.15,
        BottleneckDimension.IRREPLACEABILITY: 0.20,
        BottleneckDimension.SUPPLY_DEMAND_GAP: 0.20,
        BottleneckDimension.PRICING_POWER: 0.15,
        BottleneckDimension.TECH_BARRIER: 0.30,
    },
    "医药": {
        BottleneckDimension.SCARCITY: 0.15,
        BottleneckDimension.IRREPLACEABILITY: 0.30,
        BottleneckDimension.SUPPLY_DEMAND_GAP: 0.20,
        BottleneckDimension.PRICING_POWER: 0.20,
        BottleneckDimension.TECH_BARRIER: 0.15,
    },
    "新能源": {
        BottleneckDimension.SCARCITY: 0.20,
        BottleneckDimension.IRREPLACEABILITY: 0.15,
        BottleneckDimension.SUPPLY_DEMAND_GAP: 0.30,
        BottleneckDimension.PRICING_POWER: 0.20,
        BottleneckDimension.TECH_BARRIER: 0.15,
    },
    "消费": {
        BottleneckDimension.SCARCITY: 0.10,
        BottleneckDimension.IRREPLACEABILITY: 0.15,
        BottleneckDimension.SUPPLY_DEMAND_GAP: 0.20,
        BottleneckDimension.PRICING_POWER: 0.35,
        BottleneckDimension.TECH_BARRIER: 0.20,
    },
}


def get_industry_weights(industry: str) -> dict[BottleneckDimension, float]:
    """根据行业名称获取瓶颈评分权重。

    支持模糊匹配：如果行业名包含预设关键词则使用对应权重。
    如果行业不在预设列表中，使用 DEFAULT_WEIGHTS。

    Args:
        industry: 行业名称（如 "半导体"、"AI芯片" 等）

    Returns:
        对应行业的瓶颈维度权重字典
    """
    # 精确匹配
    if industry in INDUSTRY_WEIGHTS:
        return INDUSTRY_WEIGHTS[industry]

    # 模糊匹配：行业名包含关键词
    for key in INDUSTRY_WEIGHTS:
        if key in industry or industry in key:
            return INDUSTRY_WEIGHTS[key]

    return DEFAULT_WEIGHTS


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


def normalize_scores(reports: list[BottleneckReport]) -> list[BottleneckReport]:
    """对同一批次的评分进行 z-score 标准化，消除 LLM 评分偏差。

    对每个维度独立做 z-score，然后重新映射回 0-10 区间。
    当样本量 <3 时跳过标准化（样本太少无统计意义）。
    当某维度所有分数完全相同（sigma=0）时，利用该维度 reasoning 长度差异
    作为微扰因子，避免排名完全并列。

    Args:
        reports: 同一批次 LLM 返回的 BottleneckReport 列表

    Returns:
        原地修改后的同一列表（overall_score 会被重新计算）
    """
    if len(reports) < 3:
        return reports

    from statistics import mean, stdev

    zero_sigma_dims = 0

    for dim in BottleneckDimension:
        dim_scores: list[tuple[int, float]] = []
        dim_reasoning_lens: list[tuple[int, int]] = []
        for i, rpt in enumerate(reports):
            for s in rpt.scores:
                if s.dimension == dim.value:
                    dim_scores.append((i, s.score))
                    dim_reasoning_lens.append((i, len(s.reasoning)))
                    break

        if len(dim_scores) < 3:
            continue

        values = [v for _, v in dim_scores]
        mu = mean(values)
        sigma = stdev(values)

        if sigma < 1e-6:
            zero_sigma_dims += 1
            r_lens = [l for _, l in dim_reasoning_lens]
            r_mu = mean(r_lens) if r_lens else 1
            if r_mu < 1:
                r_mu = 1
            for idx, raw in dim_scores:
                r_len = next((l for j, l in dim_reasoning_lens if j == idx), 0)
                offset = (r_len - r_mu) / r_mu * 0.5
                offset = max(-1.0, min(1.0, offset))
                normalized = max(0.0, min(10.0, round(raw + offset, 1)))
                for s in reports[idx].scores:
                    if s.dimension == dim.value:
                        s.score = normalized
                        break
            continue

        for idx, raw in dim_scores:
            z = (raw - mu) / sigma
            normalized = max(0.0, min(10.0, round(5.0 + z * 2.0, 1)))
            for s in reports[idx].scores:
                if s.dimension == dim.value:
                    s.score = normalized
                    break

    if zero_sigma_dims >= 3:
        logger.warning(
            "瓶颈评分警告: %d/5 个维度所有节点分数完全相同 — "
            "LLM 可能直接复制了示例值，已使用 reasoning 长度微扰",
            zero_sigma_dims,
        )

    return reports


class BottleneckAnalyzer:
    """Analyzes chain nodes for bottleneck characteristics.

    支持单模型和多模型交叉评分两种模式。
    多模型时每个节点独立调用所有模型，取加权中位数合成。
    """

    LLM_TIMEOUT = 120
    MAX_CONCURRENCY = 4
    MAX_RETRIES = 2

    def __init__(
        self,
        llm: BaseChatModel | None = None,
        llms: list[tuple[BaseChatModel, str, str]] | None = None,
        weights: dict[BottleneckDimension, float] | None = None,
        language: str = "zh",
        industry: str = "",
        market: str = "",
        calibration_weights: dict[str, float] | None = None,
    ):
        if llms:
            self.llms = llms
        elif llm:
            self.llms = [(llm, "unknown", "unknown")]
        else:
            raise ValueError("必须提供 llm 或 llms 参数")

        self.calibration_weights = calibration_weights or {}
        if weights is not None:
            self.weights = weights
        elif industry:
            self.weights = get_industry_weights(industry)
        else:
            self.weights = DEFAULT_WEIGHTS
        self.industry = industry
        self.market = market
        self.language = language
        self._system_prompt = _load_prompt("bottleneck")
        self._timeout_count = 0
        self._retry_count = 0
        self._failed_nodes: list[dict] = []

    @property
    def use_cross_scoring(self) -> bool:
        return len(self.llms) >= 2

    @property
    def failed_nodes(self) -> list[dict]:
        return list(self._failed_nodes)

    async def analyze(self, graph: ChainGraph, top_n: int = 5, on_progress=None) -> list[BottleneckReport]:
        """Analyze all non-root nodes and return ranked bottleneck reports."""
        self._timeout_count = 0
        self._retry_count = 0
        self._failed_nodes = []
        self._on_progress = on_progress
        use_cross = self.use_cross_scoring
        _t0 = time.time()

        candidates = [n for n in graph.nodes if n.layer > 0]
        total = len(candidates)
        concurrency = 2 if use_cross else self.MAX_CONCURRENCY
        semaphore = asyncio.Semaphore(concurrency)

        async def _task(node, idx):
            async with semaphore:
                mode = "交叉" if use_cross else ""
                if on_progress:
                    await on_progress(f"▸ {mode}分析: {node.name} ({idx + 1}/{total})")
                if use_cross:
                    result = await self._analyze_node_multi(node.name, node.description, node.layer, graph)
                else:
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

        # z-score 标准化消除 LLM 评分偏差，然后重算加权总分
        normalize_scores(reports)
        for rpt in reports:
            rpt.overall_score = round(self._weighted_score(rpt.scores), 2)

        reports.sort(key=lambda r: r.overall_score, reverse=True)
        for i, rpt in enumerate(reports):
            rpt.rank = i + 1

        if self._timeout_count > 0:
            logger.warning(f"瓶颈分析: {self._timeout_count} 次超时放弃, {self._retry_count} 次重试")

        # ASCII 汇总，便于 Loki 检索(|= "bottleneck done")核对环节数/模型数/耗时
        _top = reports[0].overall_score if reports else 0.0
        logger.info("bottleneck done: nodes=%d scored=%d failed=%d models=%d cross=%s timeouts=%d top=%.1f elapsed=%ds",
                    total, len(reports), len(self._failed_nodes), len(self.llms), use_cross,
                    self._timeout_count, _top, int(time.time() - _t0))

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

    async def _analyze_node_multi(
        self, node_name: str, description: str, layer: int, graph: ChainGraph,
    ) -> BottleneckReport | None:
        """多模型交叉评分: 每个模型独立评分同一节点，取加权中位数合成。"""
        tasks = []
        for llm, provider, model in self.llms:
            tasks.append(self._analyze_node(node_name, description, layer, graph, llm=llm))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid: list[tuple[BottleneckReport, float]] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception) or r is None:
                continue
            _, provider, model = self.llms[i]
            cal_key = f"{provider}/{model}"
            weight = self.calibration_weights.get(cal_key, 1.0)
            valid.append((r, weight))

        if not valid:
            return None
        if len(valid) == 1:
            return valid[0][0]

        return self._merge_cross_scores(valid, node_name, description, layer)

    def _merge_cross_scores(
        self,
        results: list[tuple[BottleneckReport, float]],
        node_name: str,
        description: str,
        layer: int,
    ) -> BottleneckReport:
        """合成多模型评分: 加权中位数 + 分歧度信号。"""
        merged_scores = []

        for dim in BottleneckDimension:
            dim_data: list[tuple[float, float, str]] = []
            for report, weight in results:
                for s in report.scores:
                    if s.dimension == dim.value:
                        dim_data.append((s.score, weight, s.reasoning))
                        break

            if not dim_data:
                continue

            score = self._weighted_median([(d[0], d[1]) for d in dim_data])
            divergence = self._weighted_std([(d[0], d[1]) for d in dim_data])

            closest_idx = min(range(len(dim_data)), key=lambda i: abs(dim_data[i][0] - score))
            reasoning = dim_data[closest_idx][2]

            if divergence >= 2.0:
                score_strs = [f"{d[0]:.0f}" for d in dim_data]
                reasoning = f"[多模型分歧: 各模型={'/'.join(score_strs)}, σ={divergence:.1f}] " + reasoning

            merged_scores.append(BottleneckScore(
                dimension=dim.value,
                score=round(score, 1),
                reasoning=reasoning,
            ))

        all_insights: list[str] = []
        all_risks: list[str] = []
        for report, _ in results:
            all_insights.extend(report.key_insights)
            all_risks.extend(report.risks)
        unique_insights = list(dict.fromkeys(all_insights))[:5]
        unique_risks = list(dict.fromkeys(all_risks))[:3]

        cr3_data = [(r.cr3_estimate, w) for r, w in results if r.cr3_estimate is not None]
        hhi_data = [(r.hhi_estimate, w) for r, w in results if r.hhi_estimate is not None]
        merged_cr3 = round(self._weighted_median(cr3_data)) if cr3_data else None
        merged_hhi = round(self._weighted_median(hhi_data)) if hhi_data else None

        adjustments = self._check_hhi_consistency(merged_scores, merged_cr3, merged_hhi, node_name)

        overall = self._weighted_score(merged_scores)

        # 集中度来源：各子报告真实数据同源（同一板块缓存），取任一 akshare 来源即可
        real_report = next((r for r, _ in results if r.cr3_source == "akshare"), None)
        cr3_source = "akshare" if real_report else "llm_estimate"
        concentration_detail = real_report.concentration_detail if real_report else None
        if real_report:
            merged_cr3 = real_report.cr3_estimate
            merged_hhi = real_report.hhi_estimate

        return BottleneckReport(
            node_name=node_name,
            node_description=description,
            layer=layer,
            scores=merged_scores,
            overall_score=overall,
            key_insights=unique_insights,
            risks=unique_risks,
            cr3_estimate=merged_cr3,
            hhi_estimate=merged_hhi,
            hhi_adjustments=adjustments,
            cr3_source=cr3_source,
            concentration_detail=concentration_detail,
        )

    @staticmethod
    def _weighted_median(data: list[tuple[float, float]]) -> float:
        sorted_data = sorted(data, key=lambda x: x[0])
        total_weight = sum(w for _, w in sorted_data)
        if total_weight <= 0:
            return sorted_data[len(sorted_data) // 2][0]
        cumulative = 0.0
        for value, weight in sorted_data:
            cumulative += weight
            if cumulative >= total_weight / 2:
                return value
        return sorted_data[-1][0]

    @staticmethod
    def _weighted_std(data: list[tuple[float, float]]) -> float:
        total_w = sum(w for _, w in data)
        if total_w <= 0:
            return 0.0
        w_mean = sum(v * w for v, w in data) / total_w
        variance = sum(w * (v - w_mean) ** 2 for v, w in data) / total_w
        return variance ** 0.5

    async def _analyze_node(
        self, node_name: str, description: str, layer: int, graph: ChainGraph,
        *, llm: BaseChatModel | None = None,
    ) -> BottleneckReport | None:
        """Score a single node across all bottleneck dimensions."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        chain_context = self._build_context(node_name, graph)

        # 真实行业集中度（仅 A 股）：用板块成分股市值算 CR3/HHI，作为事实锚点覆盖 LLM 估算。
        # 东财接口间歇不可达 → 失败返回 None，静默降级回 LLM 估算，不阻断分析。
        real_conc = None
        if self.market == "a_stock":
            try:
                from bottleneck_hunter.chain.industry_concentration import compute_concentration
                real_conc = await asyncio.to_thread(compute_concentration, node_name)
            except Exception as e:
                logger.debug("真实集中度计算异常(%s): %s", node_name, e)
                real_conc = None

        real_conc_block = ""
        if real_conc:
            tops = "、".join(f"{nm}({sh}%)" for nm, sh in real_conc.get("top_companies", [])[:5] if nm)
            real_conc_block = (
                f"\n## 真实市场集中度数据（来源：东方财富板块「{real_conc['board_name']}」成分股，非估算）\n"
                f"- 该环节 A 股上市公司: {real_conc['company_count']} 家\n"
                f"- CR3={real_conc['cr3']}%  CR5={real_conc['cr5']}%  HHI={real_conc['hhi']}\n"
                + (f"- Top 公司（市值份额）: {tops}\n" if tops else "")
                + "⚠ 请【直接采用】以上真实 CR3/HHI 校准 scarcity/pricing_power，不要另行估算集中度。\n"
            )

        user_prompt = f"""{lang_note}

产业链: {graph.sector}
分析环节: {node_name}
层级: 第{layer}层
描述: {description}

{chain_context}
{real_conc_block}
请对该环节进行瓶颈分析，对以下5个维度各打0-10分，并给出理由:
{chr(10).join(f"- {d.value}: {desc}" for d, desc in DIMENSION_DESC.items())}

⚠ 重要评分原则:
- 每个环节必须根据其在产业链中的实际瓶颈特征独立打分
- 不同环节的分数必须有显著差异（真正的瓶颈环节如光刻机可能 scarcity=9，而通用材料可能只有 3）
- 严禁照搬示例中的数值，你必须根据该环节的实际情况给出不同的分数
- 上游原材料和通用设备通常得分较低（3-5分），核心技术环节得分较高（7-9分）

同时列出:
- key_insights: 关键洞察（2-3条）
- risks: 主要风险（1-2条）

返回严格 JSON（注意：下面的数值仅为格式参考，你必须根据实际情况给出完全不同的分数）:
{{
  "cr3_estimate": 65,
  "hhi_estimate": 2100,
  "scores": [
    {{"dimension": "scarcity", "score": 5, "reasoning": "根据实际情况填写"}},
    {{"dimension": "irreplaceability", "score": 3, "reasoning": "根据实际情况填写"}},
    {{"dimension": "supply_demand_gap", "score": 6, "reasoning": "根据实际情况填写"}},
    {{"dimension": "pricing_power", "score": 4, "reasoning": "根据实际情况填写"}},
    {{"dimension": "tech_barrier", "score": 7, "reasoning": "根据实际情况填写"}}
  ],
  "key_insights": ["...", "..."],
  "risks": ["...", "..."]
}}"""

        try:
            active_llm = llm or self.llms[0][0]
            messages = [
                SystemMessage(content=self._system_prompt),
                HumanMessage(content=user_prompt),
            ]
            response = None
            for attempt in range(self.MAX_RETRIES + 1):
                try:
                    response = await asyncio.wait_for(
                        active_llm.ainvoke(messages), timeout=self.LLM_TIMEOUT,
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

            data = extract_json_object(response.content)

            scores = [
                BottleneckScore(
                    dimension=s["dimension"],
                    score=s["score"],
                    reasoning=s["reasoning"],
                )
                for s in data["scores"]
            ]

            cr3 = data.get("cr3_estimate")
            hhi = data.get("hhi_estimate")
            cr3_source = "llm_estimate"
            concentration_detail = None
            # 真实数据存在时：用真实 CR3/HHI 覆盖 LLM 估算，并以真实值作为一致性校准锚点
            if real_conc:
                cr3 = int(round(real_conc["cr3"]))
                hhi = int(real_conc["hhi"])
                cr3_source = "akshare"
                concentration_detail = {
                    "board_name": real_conc["board_name"],
                    "company_count": real_conc["company_count"],
                    "cr5": real_conc["cr5"],
                    "top_companies": real_conc.get("top_companies", []),
                }
            adjustments = self._check_hhi_consistency(scores, cr3, hhi, node_name)

            overall = self._weighted_score(scores)

            return BottleneckReport(
                node_name=node_name,
                node_description=description,
                layer=layer,
                scores=scores,
                overall_score=overall,
                key_insights=data.get("key_insights", []),
                risks=data.get("risks", []),
                cr3_estimate=cr3,
                hhi_estimate=hhi,
                hhi_adjustments=adjustments,
                cr3_source=cr3_source,
                concentration_detail=concentration_detail,
            )
        except Exception:
            logger.exception(f"Failed to analyze node: {node_name}")
            return None

    @staticmethod
    def _check_hhi_consistency(
        scores: list[BottleneckScore],
        cr3: int | None,
        hhi: int | None,
        node_name: str,
    ) -> list[str]:
        """检查 LLM 的 scarcity/pricing_power 评分是否与其自身估算的 HHI/CR3 一致，不一致则修正。"""
        if cr3 is None and hhi is None:
            return []

        score_map = {s.dimension: s for s in scores}
        adjustments: list[str] = []

        scarcity = score_map.get("scarcity")
        pricing = score_map.get("pricing_power")

        if hhi is not None:
            if hhi > 2500:
                if scarcity and scarcity.score < 6:
                    old = scarcity.score
                    scarcity.score = max(6.0, scarcity.score + 2)
                    scarcity.reasoning = f"[HHI校准: HHI={hhi}>2500, {old:.0f}→{scarcity.score:.0f}] " + scarcity.reasoning
                    adjustments.append(f"scarcity {old:.0f}→{scarcity.score:.0f} (HHI={hhi}高集中度)")
                if pricing and pricing.score < 5:
                    old = pricing.score
                    pricing.score = max(5.0, pricing.score + 2)
                    pricing.reasoning = f"[HHI校准: HHI={hhi}>2500, {old:.0f}→{pricing.score:.0f}] " + pricing.reasoning
                    adjustments.append(f"pricing_power {old:.0f}→{pricing.score:.0f} (HHI={hhi}高集中度)")

            elif hhi < 1500:
                if scarcity and scarcity.score > 6:
                    old = scarcity.score
                    scarcity.score = min(6.0, scarcity.score - 2)
                    scarcity.reasoning = f"[HHI校准: HHI={hhi}<1500, {old:.0f}→{scarcity.score:.0f}] " + scarcity.reasoning
                    adjustments.append(f"scarcity {old:.0f}→{scarcity.score:.0f} (HHI={hhi}低集中度)")
                if pricing and pricing.score > 6:
                    old = pricing.score
                    pricing.score = min(6.0, pricing.score - 2)
                    pricing.reasoning = f"[HHI校准: HHI={hhi}<1500, {old:.0f}→{pricing.score:.0f}] " + pricing.reasoning
                    adjustments.append(f"pricing_power {old:.0f}→{pricing.score:.0f} (HHI={hhi}低集中度)")

        if cr3 is not None:
            if cr3 > 80:
                if scarcity and scarcity.score < 7:
                    old = scarcity.score
                    scarcity.score = max(7.0, scarcity.score + 2)
                    scarcity.reasoning = f"[CR3校准: CR3={cr3}%>80%, {old:.0f}→{scarcity.score:.0f}] " + scarcity.reasoning
                    adjustments.append(f"scarcity {old:.0f}→{scarcity.score:.0f} (CR3={cr3}%高垄断)")

            elif cr3 < 30:
                if scarcity and scarcity.score > 4:
                    old = scarcity.score
                    scarcity.score = min(4.0, scarcity.score - 2)
                    scarcity.reasoning = f"[CR3校准: CR3={cr3}%<30%, {old:.0f}→{scarcity.score:.0f}] " + scarcity.reasoning
                    adjustments.append(f"scarcity {old:.0f}→{scarcity.score:.0f} (CR3={cr3}%低集中)")

        for s in scores:
            s.score = round(max(0.0, min(10.0, s.score)), 1)

        if adjustments:
            logger.info(f"HHI一致性校准 [{node_name}]: {'; '.join(adjustments)}")

        return adjustments

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
