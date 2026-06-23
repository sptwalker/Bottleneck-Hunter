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

另外，请评估以下4个护城河维度（0-10分）:
6. patent_moat: 专利/技术壁垒（核心专利数量、技术领先程度、研发投入占比）
7. switching_cost: 客户转换成本（认证周期、定制化程度、生态锁定）
8. capacity_lead_time: 产能/交期优势（扩产周期、良率优势、设备壁垒）
9. cost_advantage: 成本优势（规模效应、工艺领先、原料自给）
并用一句话总结该公司的核心护城河（moat_reasoning）。

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
  "patent_moat": 7,
  "switching_cost": 6,
  "capacity_lead_time": 5,
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

            scores = [
                data.get("market_position", 0),
                data.get("customer_validation", 0),
                data.get("capacity_status", 0),
                data.get("financial_health", 0),
                data.get("valuation", 0),
            ]
            base_overall = sum(scores) / len(scores) if scores else 0

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

            overall = round(base_overall * 0.7 + moat_overall * 0.3, 1) if moat_overall > 0 else round(base_overall, 1)

            return SupplierScorecard(
                supplier=supplier,
                bottleneck_node=bottleneck.node_name,
                layer=bottleneck.layer,
                market_position=data.get("market_position", 0),
                customer_validation=data.get("customer_validation", 0),
                capacity_status=data.get("capacity_status", 0),
                financial_health=data.get("financial_health", 0),
                valuation=data.get("valuation", 0),
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

        # ---- 维度 5: 机构持仓 (25%) ----
        s_inst = 5.0
        if is_us and snap and snap.institution_holding_pct is not None:
            s_inst = cls._tier_score(snap.institution_holding_pct, cls.INST_TIERS_US)

        # ---- 加权汇总 ----
        raw = s_cap * 0.15 + s_analyst * 0.20 + s_vol * 0.25 + s_price * 0.15 + s_inst * 0.25

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
        if is_us and snap and snap.institution_holding_pct is not None:
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
            dim_institution=s_inst,
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
    def compute(cls, scorecard: SupplierScorecard, w_q: float = 0.4, w_a: float = 0.6) -> FinalScore:
        quality = max(0.1, min(10.0, scorecard.overall_score))
        alpha = max(0.1, scorecard.alpha.alpha_score if scorecard.alpha else 0.1)
        raw = (quality ** w_q) * (alpha ** w_a)
        final = max(0.0, min(10.0, round(raw, 2)))
        return FinalScore(
            quality_score=round(quality, 2),
            alpha_score=round(alpha, 2),
            final_score=final,
            quality_weight=w_q,
            alpha_weight=w_a,
        )

    @classmethod
    def score_all(
        cls,
        scorecards: list[SupplierScorecard],
        w_q: float = 0.4,
        w_a: float = 0.6,
    ) -> list[SupplierScorecard]:
        """为所有评分卡计算最终评分并按 final_score 排序。"""
        for sc in scorecards:
            sc.final = cls.compute(sc, w_q, w_a)
        scorecards.sort(key=lambda s: s.final.final_score, reverse=True)
        return scorecards
