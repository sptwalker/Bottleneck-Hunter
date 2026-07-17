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

from bottleneck_hunter.chain.json_utils import strip_fences
from bottleneck_hunter.chain.investability_filter import InvestabilityFilter
from bottleneck_hunter.chain.models import (
    AlphaScore,
    BottleneckReport,
    FinancialSnapshot,
    FinalScore,
    MarketRegion,
    MoatScore,
    SupplierInfo,
    SupplierScorecard,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# 数据驱动评分：用真实财务数据计算 financial_health / valuation
# LLM 对 A 股公司知识有限，各维度打分倾向 5-7 分。
# 这两个维度可以客观量化，有数据时按 70% 数据 + 30% LLM 混合。
# ---------------------------------------------------------------------------

def _data_financial_health(
    snap: FinancialSnapshot | None,
    supplier: SupplierInfo,
) -> float | None:
    """从真实财务数据计算 financial_health (0-10)。"""
    if not snap:
        return None

    components: list[tuple[float, float]] = []

    growth = snap.revenue_yoy_pct
    if growth is None:
        growth = supplier.revenue_growth
    if growth is not None:
        if growth > 50:     s = 9.5
        elif growth > 30:   s = 8.0
        elif growth > 15:   s = 7.0
        elif growth > 5:    s = 5.5
        elif growth > 0:    s = 4.5
        elif growth > -10:  s = 3.5
        else:               s = 2.0
        components.append((s, 0.30))

    gm = snap.gross_margin_pct
    if gm is None:
        gm = supplier.gross_margin
    if gm is not None:
        if gm > 60:     s = 9.5
        elif gm > 45:   s = 8.0
        elif gm > 30:   s = 6.5
        elif gm > 20:   s = 5.0
        elif gm > 10:   s = 3.5
        else:            s = 2.0
        components.append((s, 0.25))

    if snap.roe_pct is not None:
        roe = snap.roe_pct
        if roe > 25:     s = 9.5
        elif roe > 15:   s = 8.0
        elif roe > 8:    s = 6.0
        elif roe > 3:    s = 4.5
        elif roe > 0:    s = 3.5
        else:            s = 2.0
        components.append((s, 0.25))

    if snap.debt_ratio_pct is not None:
        debt = snap.debt_ratio_pct
        if debt < 25:    s = 9.0
        elif debt < 40:  s = 7.5
        elif debt < 55:  s = 6.0
        elif debt < 70:  s = 4.5
        else:            s = 3.0
        components.append((s, 0.20))

    if len(components) < 2:
        return None
    total_w = sum(w for _, w in components)
    return round(sum(s * w for s, w in components) / total_w, 1)


def _data_valuation(
    snap: FinancialSnapshot | None,
    supplier: SupplierInfo,
) -> float | None:
    """从真实财务数据计算 valuation (0-10)。低估值 → 高分。

    A 股整体估值中枢偏高（科创/创业板 PE 50-100+ 常见），
    用和美股相同的 PE 区间会导致 A 股估值分全部垫底。
    因此按市场分别设定 PE 评分区间。
    """
    if not snap:
        return None

    pe = snap.consensus_pe
    if pe is None:
        pe = supplier.pe_ratio
    if pe is None:
        return None

    if pe < 0:
        return 3.0

    growth = snap.revenue_yoy_pct
    if growth is None:
        growth = supplier.revenue_growth

    # PEG 优先（有增速时），PEG 是跨市场可比的
    if growth is not None and growth > 1:
        peg = pe / growth
        if peg < 0.5:   return 9.5
        if peg < 1.0:   return 8.0
        if peg < 1.5:   return 6.5
        if peg < 2.0:   return 5.0
        if peg < 3.0:   return 4.0
        return 2.5

    # PE-only fallback：A 股用更宽的区间
    is_astock = getattr(supplier, "market", "") == "a_stock"
    if is_astock:
        if pe < 15:   return 9.0
        if pe < 30:   return 8.0
        if pe < 50:   return 7.0
        if pe < 80:   return 5.5
        if pe < 120:  return 4.0
        if pe < 200:  return 3.0
        return 2.0
    else:
        if pe < 10:   return 9.0
        if pe < 20:   return 7.5
        if pe < 35:   return 6.0
        if pe < 50:   return 4.5
        if pe < 80:   return 3.5
        return 2.0


def _data_market_position(
    snap: FinancialSnapshot | None,
    supplier: SupplierInfo,
) -> float | None:
    """从市值规模和毛利率推算 market_position 数据锚点 (0-10)。

    大市值 → 行业龙头地位；高毛利率 → 定价权强。
    """
    cap = supplier.market_cap
    is_astock = getattr(supplier, "market", "") == "a_stock"

    components: list[tuple[float, float]] = []

    if cap is not None:
        if is_astock:
            if cap > 1000:    s = 9.0
            elif cap > 500:   s = 8.0
            elif cap > 200:   s = 7.0
            elif cap > 100:   s = 6.0
            elif cap > 50:    s = 5.0
            elif cap > 20:    s = 4.0
            else:             s = 3.0
        else:
            if cap > 200:     s = 9.0
            elif cap > 50:    s = 8.0
            elif cap > 10:    s = 7.0
            elif cap > 2:     s = 5.5
            elif cap > 0.5:   s = 4.0
            else:             s = 3.0
        components.append((s, 0.60))

    gm = None
    if snap and snap.gross_margin_pct is not None:
        gm = snap.gross_margin_pct
    elif supplier.gross_margin is not None:
        gm = supplier.gross_margin

    if gm is not None:
        if gm > 60:       s = 9.0
        elif gm > 45:     s = 8.0
        elif gm > 30:     s = 6.5
        elif gm > 20:     s = 5.0
        elif gm > 10:     s = 3.5
        else:             s = 2.5
        components.append((s, 0.40))

    if not components:
        return None

    total_w = sum(w for _, w in components)
    return round(sum(s * w for s, w in components) / total_w, 1)


def _blend(llm: float, data: float | None, data_weight: float = 0.7) -> float:
    """LLM 分与数据驱动分混合。有数据时以数据为主。"""
    if data is None:
        return llm
    return round(llm * (1 - data_weight) + data * data_weight, 1)


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
    if snap.trend and snap.trend.trend_summary:
        lines.append(f"\n## 财务趋势（近{len(snap.trend.quarters)}个季度）")
        lines.append(f"- 趋势概要: {snap.trend.trend_summary}")
        if snap.trend.revenue_acceleration is not None:
            lines.append(f"- 营收加速度: {snap.trend.revenue_acceleration:+.1f}pp")
        if snap.trend.gross_margin_trend is not None:
            lines.append(f"- 毛利率趋势: {snap.trend.gross_margin_trend:+.1f}pp")
        if snap.trend.consecutive_growth_quarters > 0:
            lines.append(f"- 连续正增长: {snap.trend.consecutive_growth_quarters}个季度")
    lines.append("\n⚠ 以上为市场API获取的真实数据，请优先基于这些数据进行财务健康和估值评分。")
    return "\n".join(lines)


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
        investability_filter: InvestabilityFilter | None = None,
    ):
        self.llm = llm
        self.language = language
        self._system_prompt = _load_prompt("supplier_eval")
        self._on_progress = None
        self._investability_filter = investability_filter or InvestabilityFilter()

    async def evaluate(
        self,
        supplier: SupplierInfo,
        bottleneck: BottleneckReport,
        financial_snapshot: FinancialSnapshot | None = None,
        batch_context: dict | None = None,
    ) -> SupplierScorecard:
        """Evaluate a single supplier against a bottleneck node."""
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"

        # 合并财务数据：优先使用 financial_snapshot 的真实数据覆盖 SupplierInfo 的 None 字段
        pe_ratio = supplier.pe_ratio
        revenue_growth = supplier.revenue_growth
        gross_margin = supplier.gross_margin
        if financial_snapshot:
            if pe_ratio is None and financial_snapshot.consensus_pe is not None:
                pe_ratio = financial_snapshot.consensus_pe
            if revenue_growth is None and financial_snapshot.revenue_yoy_pct is not None:
                revenue_growth = financial_snapshot.revenue_yoy_pct
            if gross_margin is None and financial_snapshot.gross_margin_pct is not None:
                gross_margin = financial_snapshot.gross_margin_pct

        # 构造基本面行，跳过无数据字段避免 "None" 污染 prompt
        basic_lines = [
            f"- 公司名称: {supplier.name}",
            f"- 代码: {supplier.ticker}",
            f"- 市场: {'A股' if supplier.market == 'a_stock' else '美股'}",
        ]
        cap_unit = '亿' if supplier.market == 'a_stock' else 'B'
        if supplier.market_cap is not None:
            basic_lines.append(f"- 市值: {supplier.market_cap}{cap_unit}")
        if supplier.sector:
            basic_lines.append(f"- 行业: {supplier.sector}")
        if supplier.description:
            basic_lines.append(f"- 描述: {supplier.description}")
        if pe_ratio is not None:
            basic_lines.append(f"- 市盈率(PE): {pe_ratio:.1f}")
        if revenue_growth is not None:
            basic_lines.append(f"- 营收增速: {revenue_growth:.1f}%")
        if gross_margin is not None:
            basic_lines.append(f"- 毛利率: {gross_margin:.1f}%")
        basic_block = "\n".join(basic_lines)

        financial_block = ""
        if financial_snapshot:
            financial_block = "\n\n" + _format_financial_block(financial_snapshot)

        batch_block = ""
        if batch_context and len(batch_context) > 1:
            blines = ["\n## 同批次候选概况（供参考，用于拉开差异）"]
            if batch_context.get("count"):
                blines.append(f"- 本批次共 {batch_context['count']} 家公司")
            if batch_context.get("mcap_range"):
                lo, hi = batch_context["mcap_range"]
                blines.append(f"- 市值范围: {lo:.0f}亿 ~ {hi:.0f}亿")
            if batch_context.get("pe_range"):
                lo, hi = batch_context["pe_range"]
                blines.append(f"- PE 范围: {lo:.0f} ~ {hi:.0f}")
            if batch_context.get("gm_range"):
                lo, hi = batch_context["gm_range"]
                blines.append(f"- 毛利率范围: {lo:.1f}% ~ {hi:.1f}%")
            blines.append("- 请根据该公司在同批次中的相对位置拉开评分差异")
            batch_block = "\n".join(blines)

        user_prompt = f"""{lang_note}

## 瓶颈环节
- 名称: {bottleneck.node_name}
- 描述: {bottleneck.node_description}
- 综合瓶颈得分: {bottleneck.overall_score}/10
- 关键洞察: {', '.join(bottleneck.key_insights)}

## 候选供应商
{basic_block}
{financial_block}
{batch_block}

请对该供应商进行评估，对以下5个维度各打0-10分（务必根据实际数据差异化打分，不同公司的评分应有显著区别）:
1. market_position: 市场地位（市占率、垄断/寡头地位）— 行业龙头8-10分，中等5-7分，小企业2-4分
2. customer_validation: 客户验证（是否有大客户订单、已验证）— 有明确大客户8-10分，无信息3-5分
3. capacity_status: 产能状况（利用率、扩产计划）— 产能紧张且扩产中8-10分，信息不足给4-5分
4. financial_health: 财务健康（营收增速、毛利率、现金流）— 增速>30%且毛利>40%给8-10分，增速<10%或毛利<20%给3-5分
5. valuation: 估值水平（PE/PB相对行业均值）— PE低于行业均值8-10分，高估值泡沫2-4分

另外，请评估以下4个护城河维度（0-10分）:
6. patent_moat: 专利/技术壁垒（核心专利数量、技术领先程度、研发投入占比）— 有核心专利壁垒8-10分，技术同质化2-4分
7. switching_cost: 客户转换成本（认证周期、定制化程度、生态锁定）— 认证周期长/强锁定8-10分，易替代2-4分
8. capacity_lead_time: 产能/交期优势（扩产周期、良率优势、设备壁垒）— 行业领先8-10分，无特殊优势3-5分
9. cost_advantage: 成本优势（规模效应、工艺领先、原料自给）— 显著成本优势8-10分，无优势3-5分
并用一句话总结该公司的核心护城河（moat_reasoning）。

⚠ 重要评分原则:
- 每个维度必须独立评估，分数应反映该公司的真实差异
- 如果某维度数据不足无法判断，给4-5分（中性），不要默认给高分
- 优秀企业的总分应在7-9分，普通企业5-6分，劣势企业3-4分
- 不同公司之间的评分必须有差异，避免所有公司得分相同

同时列出:
- strengths: 优势（2-3条）
- weaknesses: 风险/劣势（1-2条）

返回严格 JSON（注意：下面的数值仅为格式示例，你必须根据实际情况给出不同的分数）:
{{
  "market_position": 5,
  "customer_validation": 4,
  "capacity_status": 6,
  "financial_health": 3,
  "valuation": 7,
  "patent_moat": 4,
  "switching_cost": 5,
  "capacity_lead_time": 3,
  "cost_advantage": 6,
  "moat_reasoning": "...",
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
            text = strip_fences(text)
            data = json.loads(text)

            llm_fh = data.get("financial_health", 0)
            llm_val = data.get("valuation", 0)

            data_fh = _data_financial_health(financial_snapshot, supplier)
            data_val = _data_valuation(financial_snapshot, supplier)
            data_mp = _data_market_position(financial_snapshot, supplier)
            fh = _blend(llm_fh, data_fh)
            val = _blend(llm_val, data_val)
            mp = _blend(data.get("market_position", 0), data_mp, 0.5)

            cv = data.get("customer_validation", 0)
            cs = data.get("capacity_status", 0)
            mp_w = 1.5 if data_mp is not None else 1.0
            fh_w = 2.0 if data_fh is not None else 1.0
            val_w = 2.0 if data_val is not None else 1.0
            weighted_sum = mp * mp_w + cv * 1.0 + cs * 1.0 + fh * fh_w + val * val_w
            total_weight = mp_w + 2.0 + fh_w + val_w
            base_overall = weighted_sum / total_weight

            moat_fields = ["patent_moat", "switching_cost", "capacity_lead_time", "cost_advantage"]
            moat_scores = [data.get(f, 0) for f in moat_fields]
            moat_overall = sum(moat_scores) / len(moat_scores) if any(s > 0 for s in moat_scores) else 0
            moat = MoatScore(
                patent_moat=data.get("patent_moat", 0),
                switching_cost=data.get("switching_cost", 0),
                capacity_lead_time=data.get("capacity_lead_time", 0),
                cost_advantage=data.get("cost_advantage", 0),
                overall_moat=round(moat_overall, 1),
                moat_reasoning=data.get("moat_reasoning", ""),
            )

            overall = round(base_overall * 0.8 + moat_overall * 0.2, 1) if moat_overall > 0 else round(base_overall, 1)

            return SupplierScorecard(
                supplier=supplier,
                bottleneck_node=bottleneck.node_name,
                layer=bottleneck.layer,
                market_position=mp,
                customer_validation=data.get("customer_validation", 0),
                capacity_status=data.get("capacity_status", 0),
                financial_health=fh,
                valuation=val,
                overall_score=overall,
                strengths=data.get("strengths", []),
                weaknesses=data.get("weaknesses", []),
                financial_snapshot=financial_snapshot,
                moat=moat,
            )
        except Exception:
            logger.exception(f"Failed to evaluate supplier: {supplier.name}")
            return SupplierScorecard(
                supplier=supplier,
                bottleneck_node=bottleneck.node_name,
                layer=bottleneck.layer,
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

    @staticmethod
    def _compute_batch_context(
        suppliers: list[SupplierInfo],
        financial_map: dict[str, FinancialSnapshot],
    ) -> dict | None:
        if len(suppliers) < 2:
            return None

        mcaps = [s.market_cap for s in suppliers if s.market_cap is not None]

        pes: list[float] = []
        gms: list[float] = []
        for s in suppliers:
            snap = financial_map.get(s.ticker)
            pe = (snap.consensus_pe if snap and snap.consensus_pe else None) or s.pe_ratio
            gm = (snap.gross_margin_pct if snap and snap.gross_margin_pct else None) or s.gross_margin
            if pe is not None and pe > 0:
                pes.append(pe)
            if gm is not None:
                gms.append(gm)

        ctx: dict = {"count": len(suppliers)}
        if mcaps:
            ctx["mcap_range"] = (min(mcaps), max(mcaps))
        if pes:
            ctx["pe_range"] = (min(pes), max(pes))
        if gms:
            ctx["gm_range"] = (min(gms), max(gms))

        return ctx if len(ctx) > 1 else None

    async def evaluate_batch(
        self,
        suppliers: list[SupplierInfo],
        bottleneck: BottleneckReport,
        financial_map: dict[str, FinancialSnapshot] | None = None,
    ) -> list[SupplierScorecard]:
        """Evaluate a batch of suppliers for one bottleneck node."""
        financial_map = financial_map or {}
        batch_context = self._compute_batch_context(suppliers, financial_map)
        tasks = [
            self.evaluate(s, bottleneck, financial_map.get(s.ticker), batch_context)
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

        # ---- 可投性预筛选 ----
        filtered_map: dict[str, list[SupplierInfo]] = {}
        total_rejected = 0
        for node_name, suppliers in supplier_map.items():
            passed, rejected = self._investability_filter.filter_batch(
                suppliers, financial_map,
            )
            filtered_map[node_name] = passed
            total_rejected += len(rejected)
            if rejected and self._on_progress:
                for sup, result in rejected:
                    await self._on_progress(
                        f"✗ 可投性淘汰: {sup.name} — {'; '.join(result.reasons)}"
                    )

        # H-12：把每个瓶颈环节的"候选总数/可投数"回填到 BottleneckReport，
        # 让"高瓶颈度但 0 家可投"在最终报告可见，而非只在评估日志里一闪而过。
        for bn in bottlenecks:
            bn.total_supplier_count = len(supplier_map.get(bn.node_name, []))
            bn.investable_supplier_count = len(filtered_map.get(bn.node_name, []))
            if bn.total_supplier_count > 0 and bn.investable_supplier_count == 0:
                bn.risks.append(
                    f"⚠ 可投性缺口：该瓶颈环节 {bn.total_supplier_count} 家候选供应商全部未通过可投性门槛"
                    "（市值/毛利/成交额/上市时长），瓶颈度虽高但当前无合格可投标的")

        if total_rejected > 0:
            remaining = total_suppliers - total_rejected
            if self._on_progress:
                await self._on_progress(
                    f"── 可投性预筛选: {total_rejected} 家淘汰, {remaining} 家进入评估 ──"
                )
            total_suppliers = remaining

        if self._on_progress:
            await self._on_progress(f"── 开始评估 {total_suppliers} 家候选供应商 ──")

        for bn in bottlenecks:
            suppliers = filtered_map.get(bn.node_name, [])
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



# ---------------------------------------------------------------------------
# Alpha / 预期差评分
# ---------------------------------------------------------------------------

class AlphaScorer:
    """计算预期差 (alpha) 评分。

    核心逻辑: 5 维加权市场关注度 → alpha = 瓶颈重要性 × (1 - 关注度/10)
    维度: 市值(15%) + 分析师覆盖(20%) + 成交量动量(25%) + 近3月涨幅(15%) + 机构持仓(25%)
    """

    # A 股市值分档（亿元）
    CAP_TIERS_YI = [(50, 1), (200, 3), (500, 5), (1000, 7), (float("inf"), 9)]
    # 美股市值分档（$B）
    CAP_TIERS_B = [(2, 1), (10, 3), (50, 5), (200, 7), (float("inf"), 9)]
    # A 股分析师覆盖分档（近6月去重机构数）
    ANALYST_TIERS_A = [(3, 1), (8, 3), (15, 5), (25, 7), (float("inf"), 9)]
    # 美股分析师覆盖分档（yfinance numberOfAnalystOpinions）
    ANALYST_TIERS_US = [(5, 1), (15, 3), (25, 5), (35, 7), (float("inf"), 9)]
    # 成交量动量分档（过滤后 10日/60日均量比）
    VOL_MOMENTUM_TIERS = [(0.6, 1), (0.8, 3), (1.3, 5), (2.0, 7), (float("inf"), 9)]
    # 近3月涨幅分档（%）
    PRICE_3M_TIERS = [(-20, 1), (0, 3), (30, 5), (80, 7), (float("inf"), 9)]
    # 美股机构持仓分档（%）
    INST_TIERS_US = [(5, 1), (15, 2), (30, 4), (50, 5), (70, 7), (85, 8), (float("inf"), 9)]

    @classmethod
    def _tier_score(cls, value: float | None, tiers: list[tuple]) -> float:
        if value is None:
            return 5.0
        for threshold, score in tiers:
            if value <= threshold:
                return score
        return tiers[-1][1]

    @classmethod
    def _compute_trend_bonus(cls, snap: FinancialSnapshot | None) -> float:
        """根据财务趋势计算加分。盈利加速+毛利率扩张 → 正向加分。"""
        if not snap or not snap.trend:
            return 0.0
        trend = snap.trend
        bonus = 0.0
        if trend.revenue_acceleration is not None:
            if trend.revenue_acceleration > 5:
                bonus += 1.0
            elif trend.revenue_acceleration > 2:
                bonus += 0.5
            elif trend.revenue_acceleration < -5:
                bonus -= 0.4
        if trend.profit_acceleration is not None:
            if trend.profit_acceleration > 5:
                bonus += 0.8
            elif trend.profit_acceleration > 2:
                bonus += 0.4
            elif trend.profit_acceleration < -5:
                bonus -= 0.3
        if trend.gross_margin_trend is not None:
            if trend.gross_margin_trend > 2:
                bonus += 0.7
            elif trend.gross_margin_trend > 0.5:
                bonus += 0.3
            elif trend.gross_margin_trend < -2:
                bonus -= 0.3
        return max(-1.0, min(2.5, round(bonus, 2)))

    @classmethod
    def _compute_smart_money_bonus(cls, scorecard: SupplierScorecard) -> float:
        """根据聪明钱信号计算加分。低关注度+聪明钱看多 → 最高加分。"""
        sm = scorecard.smart_money
        if not sm:
            return 0.0
        base = sm.smart_money_score - 5.0
        return max(-1.0, min(2.0, round(base * 0.4, 2)))

    @classmethod
    def _compute_catalyst_bonus(cls, scorecard: SupplierScorecard) -> float:
        """催化剂紧迫度 → 独立加分项 0~2.0。"""
        cat = scorecard.catalyst
        if not cat or not cat.events:
            return 0.0
        return round(cat.urgency_score / 10 * 2.0, 2)

    @classmethod
    def compute(
        cls,
        scorecard: SupplierScorecard,
        bottleneck_score: float,
    ) -> AlphaScore:
        """为一张评分卡计算 alpha。"""
        snap = scorecard.financial_snapshot
        cap = scorecard.supplier.market_cap
        is_us = scorecard.supplier.market == MarketRegion.US_STOCK

        # ---- 维度 1: 市值规模 (15%) ----
        cap_tiers = cls.CAP_TIERS_B if is_us else cls.CAP_TIERS_YI
        s_cap = cls._tier_score(cap, cap_tiers)

        # ---- 维度 2: 分析师/机构覆盖 (20%) ----
        analyst_tiers = cls.ANALYST_TIERS_US if is_us else cls.ANALYST_TIERS_A
        s_analyst = 5.0
        if snap and snap.analyst_report_count is not None:
            s_analyst = cls._tier_score(snap.analyst_report_count, analyst_tiers)

        # ---- 维度 3: 成交量动量 (25%) ----
        s_vol = 5.0
        if snap and snap.volume_ratio is not None:
            s_vol = cls._tier_score(snap.volume_ratio, cls.VOL_MOMENTUM_TIERS)
            if snap.consecutive_volume_days >= 3:
                s_vol = min(s_vol + 1, 9)

        # ---- 维度 4: 近3月涨幅 (15%) ----
        s_price = 5.0
        if snap and snap.price_change_3m_pct is not None:
            s_price = cls._tier_score(snap.price_change_3m_pct, cls.PRICE_3M_TIERS)

        # ---- 维度 5: 机构持仓 (25%, 仅美股) ----
        s_inst = 5.0
        has_inst = False
        if is_us and snap and snap.institution_holding_pct is not None:
            s_inst = cls._tier_score(snap.institution_holding_pct, cls.INST_TIERS_US)
            has_inst = True

        # ---- 加权汇总 ----
        if has_inst:
            raw = s_cap * 0.15 + s_analyst * 0.20 + s_vol * 0.25 + s_price * 0.15 + s_inst * 0.25
        else:
            raw = s_cap * 0.20 + s_analyst * 0.267 + s_vol * 0.333 + s_price * 0.20

        ipo_bonus = 0
        if snap and snap.days_since_ipo is not None and snap.days_since_ipo < 365:
            ipo_bonus = 2

        market_attention = max(2.0, min(10.0, round(raw + ipo_bonus, 1)))
        information_gap = round(10.0 - market_attention, 1)

        # ---- Alpha 计算 (√压缩 + 独立因子加法) ----
        from math import sqrt
        raw_gap = bottleneck_score * (1 - market_attention / 10)
        base_alpha = round(min(5.0, sqrt(max(0, raw_gap)) * 2.0), 2)

        trend_bonus = cls._compute_trend_bonus(snap)
        smart_money_bonus = cls._compute_smart_money_bonus(scorecard)
        catalyst_bonus = cls._compute_catalyst_bonus(scorecard)

        vp_discount = 1.0
        if snap and snap.price_change_1m_pct is not None and snap.volume_ratio is not None:
            if snap.price_change_1m_pct > 20 and snap.volume_ratio < 0.8:
                vp_discount = 0.8

        alpha = round(base_alpha * vp_discount + catalyst_bonus + trend_bonus + smart_money_bonus, 1)
        alpha = min(10.0, max(0.0, alpha))

        # ---- reasoning ----
        parts = []
        if cap is not None:
            cap_unit = "$B" if is_us else "亿"
            parts.append(f"市值{cap}{cap_unit}")
        if snap and snap.analyst_report_count is not None:
            count_label = "分析师" if is_us else "覆盖机构"
            parts.append(f"{count_label}{snap.analyst_report_count}")
        if snap and snap.volume_ratio is not None:
            parts.append(f"量比{snap.volume_ratio:.2f}")
            if snap.consecutive_volume_days >= 3:
                parts.append("连续放量")
        if snap and snap.price_change_3m_pct is not None:
            parts.append(f"3月涨幅{snap.price_change_3m_pct:+.0f}%")
        if has_inst:
            parts.append(f"机构持仓{snap.institution_holding_pct:.0f}%")
        parts.append(f"关注度{market_attention}")
        parts.append(f"瓶颈分{bottleneck_score}")
        parts.append(f"基础α={base_alpha}")
        if ipo_bonus:
            parts.append("新股+2")
        if catalyst_bonus > 0:
            parts.append(f"催化剂+{catalyst_bonus:.1f}")
            if scorecard.catalyst and scorecard.catalyst.summary:
                parts.append(scorecard.catalyst.summary[:30])
        if trend_bonus != 0:
            parts.append(f"趋势{'加' if trend_bonus > 0 else '减'}分{trend_bonus:+.1f}")
            if snap and snap.trend and snap.trend.trend_summary:
                parts.append(snap.trend.trend_summary)
        if smart_money_bonus != 0:
            parts.append(f"聪明钱{'加' if smart_money_bonus > 0 else '减'}分{smart_money_bonus:+.1f}")
            if scorecard.smart_money and scorecard.smart_money.details:
                parts.append(scorecard.smart_money.details[0])
        if vp_discount < 1.0:
            parts.append("⚠无量上涨折扣×0.8")
        reasoning = "，".join(parts) + f" → alpha={alpha}"

        return AlphaScore(
            market_attention=market_attention,
            information_gap=information_gap,
            alpha_score=alpha,
            trend_bonus=trend_bonus,
            smart_money_bonus=smart_money_bonus,
            catalyst_bonus=catalyst_bonus,
            dim_cap=s_cap,
            dim_analyst=s_analyst,
            dim_volume=s_vol,
            dim_price=s_price,
            dim_institution=s_inst if has_inst else None,
            ipo_bonus=ipo_bonus,
            vp_discount=vp_discount,
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


# ---------------------------------------------------------------------------
# 统一最终评分
# ---------------------------------------------------------------------------

class FinalScorer:
    """统一最终评分：quality^w_q × alpha^w_a（几何加权均值）。

    quality = overall_score（纯 LLM 质量评估），alpha = 现有 alpha_score。
    两者完全正交，无维度重叠。
    """

    @classmethod
    def compute(cls, scorecard: SupplierScorecard, w_q: float = 0.55, w_a: float = 0.45) -> FinalScore:
        quality = max(0.1, min(10.0, scorecard.overall_score))
        alpha = max(0.1, scorecard.alpha.alpha_score if scorecard.alpha else 0.1)
        raw = (quality ** w_q) * (alpha ** w_a)
        final = max(0.0, min(10.0, round(raw, 2)))

        # 保留已有的 credibility 和 quality_adjusted（如果 FactCheck 已经设置）
        existing_final = scorecard.final
        credibility = existing_final.credibility if existing_final else None
        quality_adjusted = existing_final.quality_adjusted if existing_final else None

        return FinalScore(
            quality_score=round(quality, 2),
            alpha_score=round(alpha, 2),
            final_score=final,
            quality_weight=w_q,
            alpha_weight=w_a,
            credibility=credibility,
            quality_adjusted=quality_adjusted,
        )

    @classmethod
    def score_all(
        cls,
        scorecards: list[SupplierScorecard],
        w_q: float = 0.55,
        w_a: float = 0.45,
    ) -> list[SupplierScorecard]:
        """为所有评分卡计算最终评分并按 final_score 排序。"""
        for sc in scorecards:
            sc.final = cls.compute(sc, w_q, w_a)
        scorecards.sort(key=lambda s: s.final.final_score, reverse=True)
        return scorecards
