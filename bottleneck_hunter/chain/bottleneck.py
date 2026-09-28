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

from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Default weights for overall score calculation
# 去重：scarcity 与 irreplaceability 高度相关(ρ≈0.8)，二者合计从 0.50 降到 0.40，
# 释放的 0.10 分给相对正交的 supply_demand_gap(+0.05) 与 tech_barrier(+0.05)，
# 避免"稀缺+不可替代"这对近似重复因子主导综合分。分行业权重见 INDUSTRY_WEIGHTS。
DEFAULT_WEIGHTS: dict[BottleneckDimension, float] = {
    BottleneckDimension.SCARCITY: 0.20,
    BottleneckDimension.IRREPLACEABILITY: 0.20,
    BottleneckDimension.SUPPLY_DEMAND_GAP: 0.25,
    BottleneckDimension.PRICING_POWER: 0.15,
    BottleneckDimension.TECH_BARRIER: 0.20,
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

    # 真实集中度数据源的连续失败熔断阈值（类级共享，见 _fetch_real_concentration）。
    _CONC_FAIL_LIMIT = 12
    _real_conc_consecutive_fails = 0

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
        self._last_fail_reason = ""  # 最近一次节点分析失败的中文原因(供上层弹窗判定主模型失败)

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
        self._last_fail_reason = ""
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
        # z-score 标准化消除 LLM 评分偏差，然后重算加权总分；随后按集中度锚定
        # scarcity/pricing_power —— 校准只能在标准化之后，见 _calibrate_concentration。
        normalize_scores(reports)
        self._calibrate_concentration(reports)
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

        # 与 analyze() 同款收口：补充分析的节点此前完全不走 z-score（因此保留着
        # 未标准化的原始 LLM 评分，与首批节点不同尺度），且校准只做一次、不重放。
        # 这里让两条路径的分数落在同一套规则下，否则同一次分析里两批节点不可比。
        normalize_scores(reports)
        self._calibrate_concentration(reports)
        for rpt in reports:
            rpt.overall_score = round(self._weighted_score(rpt.scores), 2)

        self._on_progress = None
        return reports

    async def _fetch_real_concentration(self, node_name: str) -> dict | None:
        """取 A 股真实集中度（akshare），失败返回 None 降级回 LLM 估算。

        「失败」分两种，处理方式不同：
        - 单节点没匹配上板块 → 正常的降级，静默；
        - 板块列表接口本身拉不到（东财 `stock_board_industry_name_em` 实测
          连续 5 次 RemoteDisconnected）→ **本批直接放弃**。这是最贵的调用：
          每个节点都要等 akshare 内部重试耗尽约 5-6s，上百个节点就是几分钟白白
          烧在注定失败的请求上，拉长整条分析链路。

        连续失败数用类级别累计（实例是 per-analyze 的，实例属性会随分析结束清零，
        起不到熔断作用）；成功一次即清零，给恢复留路。
        """
        from bottleneck_hunter.chain.industry_concentration import (
            ProbeFailure,
            compute_concentration,
        )

        if BottleneckAnalyzer._real_conc_consecutive_fails >= self._CONC_FAIL_LIMIT:
            return None
        try:
            result = await asyncio.to_thread(compute_concentration, node_name)
        except ProbeFailure as e:
            BottleneckAnalyzer._real_conc_consecutive_fails += 1
            if BottleneckAnalyzer._real_conc_consecutive_fails == self._CONC_FAIL_LIMIT:
                logger.warning(
                    "真实集中度数据源连续 %d 个节点不可达，本批放弃（降级回 LLM 估算）: %s",
                    self._CONC_FAIL_LIMIT, e,
                )
            return None
        except Exception:
            # 东财接口间歇不可达 → 静默降级回 LLM 估算，不阻断分析
            logger.debug("真实集中度计算失败: %s", node_name, exc_info=True)
            return None
        # 成功即清零，给恢复留路（此前清零写在 else 分支里，而 try 里已经 return，
        # 那段代码永远不可达 —— 计数器只增不减，一次抖动会污染整个进程）。
        BottleneckAnalyzer._real_conc_consecutive_fails = 0
        return result

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

        # 集中度来源：各子报告真实数据同源（同一板块缓存），取任一 akshare 来源即可
        real_report = next((r for r, _ in results if r.cr3_source == "akshare"), None)
        cr3_source = "akshare" if real_report else "llm_estimate"
        concentration_detail = real_report.concentration_detail if real_report else None
        if real_report:
            merged_cr3 = real_report.cr3_estimate
            merged_hhi = real_report.hhi_estimate

        # 此处**不**校准。（此前在这里跑过一次 `_check_hhi_consistency`，但它的结果
        # 随后会被 `normalize_scores` 的 `5+2z` 整维重写 —— 分数白改，reasoning 里
        # 那句「[HHI校准…]」却留了下来，于是报告同时出现「校准到 5」和实际 1.8，
        # 生产实测 30 条校准里 28 条如此。校准统一在标准化之后做一次，见
        # `_calibrate_concentration`。）
        overall = self._weighted_score(merged_scores)

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
        # 东财接口间歇不可达 → 失败返回 None 降级回 LLM 估算；连续失败则熔断（见
        # `_fetch_real_concentration`），不再逐节点烧在注定失败的请求上。
        real_conc = None
        if self.market == "a_stock":
            real_conc = await self._fetch_real_concentration(node_name)

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
        else:
            # 取不到真实数据时必须**明说**。此前这里只留空：LLM 看提示词里的示例
            # HHI=1800，会误以为自己拿到了真实值，集中度就照着示例编了 —— 而下游
            # `_check_hhi_consistency` 会把这些编出来的数当锚点去改分。
            real_conc_block = (
                "\n## 市场集中度：无真实数据\n"
                "本次未取到该环节的真实集中度数据。CR3/HHI 请基于你的知识**保守估算**，"
                "并在 reasoning 里注明「估算」。\n"
            )

        # 供需缺口（权重最高 0.25）此前是唯一既无数据锚、也无「无数据」声明的维度（P2-2）。
        # 锚点用 6A 采集的扩产周期 —— system prompt 的 supply_demand_gap 刻度本来就按
        # 扩产周期分档，缺的只是把这个事实递过去。取不到时必须明说，理由同上方集中度块。
        # ponytail: 审查建议的 akshare 财报/板块锚在生产不可达（实测 4/4 None、各 22s），
        # 且 24/32 分析是美股；真实产能利用率数据源接通后再加第二路锚。
        graph_node = graph.get_node(node_name)
        lead = graph_node.capacity_lead_time_months if graph_node else None
        if lead:
            sdg_block = (
                "\n## 供需缺口锚点\n"
                f"- 拆解阶段估计该环节产能扩张周期约 {lead} 个月（估计值，非披露数据）\n"
                "请对照 supply_demand_gap 刻度中的「扩产周期」一档打分；与之不符须在 reasoning 说明理由。\n"
            )
        else:
            sdg_block = (
                "\n## 供需缺口：无数据锚\n"
                "本环节没有产能利用率、扩产周期等数据。supply_demand_gap 请**保守打分**，"
                "并在 reasoning 里注明「估算」。\n"
            )

        user_prompt = f"""{lang_note}

产业链: {graph.sector}
分析环节: {node_name}
层级: 第{layer}层
描述: {description}

{chain_context}
{real_conc_block}{sdg_block}
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
                    # 超时/切换归 FallbackChatModel 内部；全部候选超时才抛 TimeoutError。
                    response = await active_llm.ainvoke(messages)
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
                        self._last_fail_reason = "请求超时"
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
                        try:
                            from bottleneck_hunter.llm_clients.fallback import classify_reason
                            self._last_fail_reason = classify_reason(e)
                        except Exception:  # noqa: BLE001
                            self._last_fail_reason = "调用异常"
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
            # 此处不校准：真实值覆盖已完成，校准统一留给标准化之后的
            # `_calibrate_concentration`（跑在这里会被 z-score 抹掉）。
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
        cr3_source: str = "llm_estimate",
    ) -> list[str]:
        """用集中度锚定 scarcity/pricing_power，并保证 reasoning 那句戳与最终分数一致。

        校准幅度随 `cr3_source` 缩放：LLM 自估的 HHI/CR3 与真实板块成分股算出的
        不确定性差一个量级，却曾同权同效。`llm_estimate` 时幅度减半（至少 1 分），
        且文本里标注来源，避免读者把估算值当作事实。

        **分数先全部算完，戳最后统一写**。HHI 与 CR3 会先后命中同一维度（HHI 先把
        scarcity 抬到 6，CR3 再抬到 8）；若每条规则各自写戳，先写的那条记的就是中间
        值 6 —— 读者只看到第一个戳，于是「文本说 6、分数是 8」。生产里 LLM 自估的
        CR3/HHI 常不互洽，两条规则同时命中是常态，不是罕见分支。

        Returns: 描述**实际生效**改动的条目（净变化为零的维度不记）。
        """
        if cr3 is None and hhi is None:
            return []

        # 真实数据(akshare)幅度 2 分，LLM 估算减半 → 1 分（见 P1-12）
        step = 2.0 if cr3_source == "akshare" else 1.0
        tag = "" if cr3_source == "akshare" else "(估算)"

        score_map = {s.dimension: s for s in scores}
        # 维度 -> (首次命中前的分数, [命中的规则标签])，戳留到全部规则跑完再写
        hits: dict[str, tuple[float, list[str]]] = {}

        def _hit(dim: BottleneckScore | None, target: float, label: str) -> None:
            if dim is None:
                return
            prev = hits.setdefault(dim.dimension, (dim.score, []))
            dim.score = target
            prev[1].append(label)

        scarcity = score_map.get("scarcity")
        pricing = score_map.get("pricing_power")

        if hhi is not None:
            if hhi > 2500:
                if scarcity and scarcity.score < 6:
                    _hit(scarcity, max(6.0, scarcity.score + step), f"HHI校准{tag}: HHI={hhi}>2500")
                if pricing and pricing.score < 5:
                    _hit(pricing, max(5.0, pricing.score + step), f"HHI校准{tag}: HHI={hhi}>2500")

            elif hhi < 1500:
                if scarcity and scarcity.score > 6:
                    _hit(scarcity, min(6.0, scarcity.score - step), f"HHI校准{tag}: HHI={hhi}<1500")
                if pricing and pricing.score > 6:
                    _hit(pricing, min(6.0, pricing.score - step), f"HHI校准{tag}: HHI={hhi}<1500")

        if cr3 is not None:
            if cr3 > 80:
                if scarcity and scarcity.score < 7:
                    _hit(scarcity, max(7.0, scarcity.score + step), f"CR3校准{tag}: CR3={cr3}%>80%")

            elif cr3 < 30:
                if scarcity and scarcity.score > 4:
                    _hit(scarcity, min(4.0, scarcity.score - step), f"CR3校准{tag}: CR3={cr3}%<30%")

        for s in scores:
            s.score = round(max(0.0, min(10.0, s.score)), 1)

        adjustments: list[str] = []
        for s in scores:
            rec = hits.get(s.dimension)
            if rec is None:
                continue
            old, labels = rec
            if s.score == round(old, 1):
                continue  # 两条规则方向相反、抵消回原值 —— 分数没动，不写校准戳
            # 用维度的字符串值（"pricing_power"）而非 enum repr —— 这条串会进日志，
            # str(BottleneckDimension.PRICING_POWER) 是 "BottleneckDimension.PRICING_POWER"。
            name = getattr(s.dimension, "value", s.dimension)
            # `:g` 而非 `:.0f`：分数可为 3.9，写「→4」就是在重犯本条要修的那种
            # 「文本说的和旁边分数不是一回事」。整数时 `:g` 仍写 "8"。
            old_s, new_s = f"{round(old, 1):g}", f"{s.score:g}"
            s.reasoning = f"[{'; '.join(labels)}, {old_s}→{new_s}] " + s.reasoning
            adjustments.append(f"{name} {old_s}→{new_s} ({'; '.join(labels)})")

        if adjustments:
            logger.info(f"HHI一致性校准 [{node_name}] (source={cr3_source}): {'; '.join(adjustments)}")

        return adjustments

    def _calibrate_concentration(
        self, reports: list[BottleneckReport],
    ) -> int:
        """用真实/估算的集中度锚定 scarcity/pricing_power —— **标准化之后**跑，是唯一的校准点。

        校准必须在 `normalize_scores` **之后**：那里用 `5 + 2z` 把整维重写成另一套
        尺度，任何跑在它前面的校准都被覆盖掉。此前正是如此 —— `_analyze_node` 与
        `_merge_sub_reports` 各自校准过一次，随后被 z-score 抹平，而 reasoning 里
        那句「[HHI校准: HHI=1200<1500, 7→5]」留了下来，报告同时出现「校准到 5」和
        实际 1.8（生产实测 30 条校准里 28 条如此，比不校准更误导）。

        现在那两处不再校准（见各自注释），只此一处。分数已被标准化到共同尺度，
        此时按 HHI/CR3 锚定正是想要的效果。

        幂等：`_check_hhi_consistency` 每个分支都**自证伪** —— 只在 `score < 6` 时
        抬到 `≥6`，只在 `score > 6` 时压到 `≤6`。前一次已把它推到界线另一侧，
        再跑必然一个分支都不命中。

        只动确实带集中度数据的节点（生产占比 29/8437）。不做「整批跳过该维度」的
        批级锚定：那会为 29 个节点改变 8000+ 个节点的打分，代价远大于收益。

        Returns: **分数真被改动的**节点数（分支命中但落点未变的不计）。
        """
        n = 0
        for rpt in reports:
            if rpt.cr3_estimate is None and rpt.hhi_estimate is None:
                continue
            before = tuple(s.score for s in rpt.scores)
            adjustments = self._check_hhi_consistency(
                rpt.scores, rpt.cr3_estimate, rpt.hhi_estimate, rpt.node_name, rpt.cr3_source,
            )
            if not adjustments:
                continue  # 有集中度数据但无需校准
            if tuple(s.score for s in rpt.scores) == before:
                continue  # 两条规则方向相反、抵消回原值 —— 分数没变，不记为已校准
            # 只在分数真动了时才记：否则 hhi_adjustments 会写满「scarcity 4→6」这类
            # 根本没生效的条目（生产 30 条校准文本里正有这种误导）。
            rpt.hhi_adjustments = list(adjustments)
            n += 1
        return n

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

        # 拆解阶段已经知道、但此前**只有写没有读**的约束事实（P2-1）。两处来源：
        #
        # 1. `IndustryNode` 的结构化字段（本轮新增）—— 干净、可直接判分；
        # 2. `ChainLink.notes` 的自由文本 —— 拆解 prompt 一直在要这个字段，
        #    生产 14064 条 link 里 **13960 条有内容**，全是约束事实本身
        #    （"高端纯化填料被 GE/Waters 等外资厂商垄断"、"高端品种高度依赖进口"）。
        #    它被存进 DB 后**全仓没有一个读取方**。不接这一段，那些知识就得等
        #    用户重新拆解才会经新字段回来；接上它，缓存链（14 天）立刻受益。
        #
        # ⚠ 确实**没有** `IndustryNode.notes` 这个字段（`ChainLink` 才有）。
        # 先前审查与本人初稿都写成「LLM 在节点的 notes 里带一句」——错了，
        # 故此处按 link 取。缺一项就不提那一项（宁缺勿编）。
        facts = []
        node = graph.get_node(node_name)
        if node is not None:
            if node.supply_structure:
                facts.append(f"供应结构: {node.supply_structure}")
            # 扩产周期不在这里：它是 supply_demand_gap 的锚点，见 `_analyze_node` 的 sdg_block。
            if node.qualification_cycle_months:
                facts.append(f"新进入者认证周期: 约 {node.qualification_cycle_months} 个月")
            if node.geo_concentration:
                facts.append(f"地理集中度: {node.geo_concentration}")
            if node.export_control_risk:
                facts.append(f"出口管制/政策风险: {node.export_control_risk}")

        # 本环节 → 下游的 link，notes 讲的就是**本环节**为什么难替代。
        # notes 与 alternatives 各自独立判断：notes 空不代表 alternatives 不存在
        # （生产 14064 条 link 里有 104 条只有 alternatives 没有 notes）。
        #
        # 一个节点可有多条出边（生产 93% ≤3 条，但「控制系统」这类通用名会在
        # 多个子树里撞名，最多 24 条）。逐条照搬会出现重复行和互相矛盾的
        # 「替代方案 1 个 / 2 个 / 3 个」，所以：notes 去重、alternatives 合成一行。
        # ponytail: 硬上限 5 条 notes，覆盖 97% 节点；撞名节点要真正分开得在拆解侧去重命名
        notes: list[str] = []
        alts: set[int] = set()
        for link in graph.links:
            if link.upstream != node_name:
                continue
            note = (link.notes or "").strip()
            if note and note not in notes:
                notes.append(note)
            if link.alternatives:
                alts.add(link.alternatives)
        facts.extend(f"拆解备注: {n}" for n in notes[:5])
        if len(alts) == 1:
            facts.append(f"已知替代方案: {alts.pop()} 个")
        elif alts:
            facts.append(f"已知替代方案: {min(alts)}~{max(alts)} 个（不同下游口径不一）")

        if facts:
            lines.append("## 该环节的已知结构性事实（拆解阶段采集，请据此判断）")
            lines.extend(facts)
        return "\n".join(lines)
