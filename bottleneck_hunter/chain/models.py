"""Data models for industry chain analysis."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_serializer


class LayerType(str, Enum):
    END_PRODUCT = "end_product"
    ASSEMBLY = "assembly"
    COMPONENT = "component"
    SUB_COMPONENT = "sub_component"
    MATERIAL = "material"
    RAW_MATERIAL = "raw_material"
    EQUIPMENT = "equipment"


class MarketRegion(str, Enum):
    A_STOCK = "a_stock"  # China A-share
    US_STOCK = "us_stock"
    ALL = "all"


class IndustryNode(BaseModel):
    """A single node in the industry chain."""

    name: str = Field(description="Node name, e.g. '光模块', '磷化铟衬底'")
    description: str = Field(description="What this node does in the chain")
    layer: int = Field(description="Depth from end product (0 = end product)")
    layer_type: LayerType
    function: str = Field(description="Technical function in the supply chain")
    key_parameters: list[str] = Field(default_factory=list, description="Key specs/parameters")
    upstream_deps: list[str] = Field(default_factory=list, description="Names of upstream nodes this depends on")
    downstream_deps: list[str] = Field(default_factory=list, description="Names of downstream nodes that depend on this")
    representative_companies: list[dict] = Field(
        default_factory=list,
        description="Representative companies: [{name, code (stock ticker, may be empty)}]",
    )

    @field_validator("key_parameters", "upstream_deps", "downstream_deps", mode="before")
    @classmethod
    def _ensure_str_list(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split("、") if s.strip()] if "、" in v else [v]
        if not isinstance(v, list):
            return []
        return [str(item) for item in v]

    @field_validator("representative_companies", mode="before")
    @classmethod
    def _normalize_companies(cls, v):
        if not isinstance(v, list):
            return []
        result = []
        for item in v:
            if isinstance(item, dict):
                result.append({"name": item.get("name", ""), "code": item.get("code", "")})
            elif isinstance(item, str) and item.strip():
                result.append({"name": item.strip(), "code": ""})
        return result


class ChainLink(BaseModel):
    """An edge connecting two nodes in the industry chain."""

    upstream: str = Field(description="Upstream node name")
    downstream: str = Field(description="Downstream node name")
    dependency: float = Field(ge=0, le=1, description="How critical this link is (0=optional, 1=irreplaceable)")
    alternatives: int = Field(ge=0, description="Number of known alternatives")
    notes: str = ""


class ChainGraph(BaseModel):
    """Complete industry chain graph for a sector."""

    sector: str = Field(description="Target sector, e.g. 'GPU/AI算力'")
    end_product: str = Field(description="Root product, e.g. 'GPU'")
    nodes: list[IndustryNode] = Field(default_factory=list)
    links: list[ChainLink] = Field(default_factory=list)
    max_depth: int = Field(default=3, description="How many layers were decomposed")
    metadata: dict = Field(default_factory=dict)
    version: int = Field(default=1, description="产业链版本号")
    created_at: str = Field(default="", description="创建时间 ISO 格式")
    model_used: str = Field(default="", description="拆解使用的 LLM 模型名称")

    def get_node(self, name: str) -> Optional[IndustryNode]:
        return next((n for n in self.nodes if n.name == name), None)

    def get_nodes_at_layer(self, layer: int) -> list[IndustryNode]:
        return [n for n in self.nodes if n.layer == layer]

    def get_upstream(self, node_name: str) -> list[IndustryNode]:
        """Get all nodes directly upstream of the given node."""
        upstream_names = [
            link.upstream for link in self.links if link.downstream == node_name
        ]
        return [n for n in self.nodes if n.name in upstream_names]

    def get_downstream(self, node_name: str) -> list[IndustryNode]:
        """Get all nodes directly downstream of the given node."""
        downstream_names = [
            link.downstream for link in self.links if link.upstream == node_name
        ]
        return [n for n in self.nodes if n.name in downstream_names]


class BottleneckDimension(str, Enum):
    SCARCITY = "scarcity"              # 稀缺性
    IRREPLACEABILITY = "irreplaceability"  # 不可替代性
    SUPPLY_DEMAND_GAP = "supply_demand_gap"  # 供需缺口
    PRICING_POWER = "pricing_power"    # 定价权/涨价能力
    TECH_BARRIER = "tech_barrier"      # 技术壁垒


class BottleneckScore(BaseModel):
    """Score for a single dimension of bottleneck analysis."""

    dimension: BottleneckDimension
    score: float = Field(ge=0, le=10)
    reasoning: str


class BottleneckReport(BaseModel):
    """Bottleneck analysis result for a single chain node."""

    node_name: str
    node_description: str
    layer: int
    scores: list[BottleneckScore]
    overall_score: float = Field(ge=0, le=10, description="Weighted average")
    rank: Optional[int] = None
    key_insights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    cr3_estimate: Optional[int] = Field(None, ge=0, le=100, description="LLM 估算的 CR3 市场集中度(%)")
    hhi_estimate: Optional[int] = Field(None, ge=0, le=10000, description="LLM 估算的 HHI 赫芬达尔指数")
    hhi_adjustments: list[str] = Field(default_factory=list, description="HHI 一致性校验的调整记录")
    # 可投性统计（H-12）：让"高瓶颈但无可投标的"在报告层可见，而非只在评估日志里
    total_supplier_count: int = Field(0, description="该瓶颈环节检索到的候选供应商总数")
    investable_supplier_count: int = Field(0, description="其中通过可投性过滤（市值/毛利/成交额/上市时长）的数量")

    model_config = {"use_enum_values": True}


class SupplierInfo(BaseModel):
    """A candidate supplier company."""

    name: str
    name_cn: str = Field(default="", description="公司中文名称")
    ticker: str
    market: MarketRegion
    market_cap: Optional[float] = Field(None, description="Market cap in local currency (亿 for A-stock, $B for US)")
    sector: str
    description: str
    market_share: Optional[float] = Field(None, description="Market share percentage if known")
    key_products: list[str] = Field(default_factory=list)
    revenue_growth: Optional[float] = None
    gross_margin: Optional[float] = None
    pe_ratio: Optional[float] = None
    institution_holding_pct: Optional[float] = None
    source: str = Field(default="llm", description="候选来源: llm / akshare / chain")


class QuarterlyDataPoint(BaseModel):
    """单季度财务数据点。"""

    report_date: str = Field(default="", description="报告期 e.g. 2025-03-31")
    revenue_yi: Optional[float] = Field(None, description="营业总收入(亿)")
    net_profit_yi: Optional[float] = Field(None, description="归母净利润(亿)")
    gross_margin_pct: Optional[float] = Field(None, description="销售毛利率(%)")
    roe_pct: Optional[float] = Field(None, description="净资产收益率(%)")
    revenue_yoy_pct: Optional[float] = Field(None, description="营收同比增速(%)")
    net_profit_yoy_pct: Optional[float] = Field(None, description="净利润同比增速(%)")


class FinancialTrend(BaseModel):
    """多季度财务趋势分析结果。"""

    quarters: list[QuarterlyDataPoint] = Field(default_factory=list, description="近N个季度数据，按时间降序")
    revenue_acceleration: Optional[float] = Field(None, description="营收加速度: 最近2Q平均增速 - 前2Q平均增速")
    gross_margin_trend: Optional[float] = Field(None, description="毛利率趋势: 最近2Q均值 - 前2Q均值（百分点）")
    consecutive_growth_quarters: int = Field(default=0, description="连续营收正增长季度数")
    profit_acceleration: Optional[float] = Field(None, description="净利润加速度: 最近2Q平均增速 - 前2Q平均增速")
    trend_summary: str = Field(default="", description="趋势一句话摘要")


class FinancialSnapshot(BaseModel):
    """真实财务数据快照，来自市场 API 而非 LLM。"""

    data_source: str = Field(default="", description="akshare_ths / yfinance / tencent")
    report_date: str = Field(default="", description="最近一期财报日期 e.g. 2025-12-31")

    revenue_yi: Optional[float] = Field(None, description="营业总收入(亿)")
    revenue_yoy_pct: Optional[float] = Field(None, description="营收同比增速(%)")
    net_profit_yi: Optional[float] = Field(None, description="归母净利润(亿)")
    net_profit_yoy_pct: Optional[float] = Field(None, description="净利润同比增速(%)")
    gross_margin_pct: Optional[float] = Field(None, description="销售毛利率(%)")
    roe_pct: Optional[float] = Field(None, description="净资产收益率(%)")
    debt_ratio_pct: Optional[float] = Field(None, description="资产负债率(%)")
    cashflow_per_share: Optional[float] = Field(None, description="每股经营现金流")

    analyst_report_count: Optional[int] = Field(None, description="近期研报覆盖数")
    analyst_rating: Optional[str] = Field(None, description="最新机构评级")
    consensus_eps: Optional[float] = Field(None, description="一致预期 EPS（当年）")
    consensus_pe: Optional[float] = Field(None, description="一致预期 PE（当年）")

    trend: Optional[FinancialTrend] = Field(None, description="多季度财务趋势")

    volume_ratio: Optional[float] = Field(None, description="成交量动量: 过滤后10日均量/60日均量")
    price_change_3m_pct: Optional[float] = Field(None, description="近3月涨幅(%)")
    price_change_1m_pct: Optional[float] = Field(None, description="近1月涨幅(%)")
    institution_holding_pct: Optional[float] = Field(None, description="机构持仓占流通股比例(%)")
    consecutive_volume_days: int = Field(default=0, description="连续放量天数(日成交量>60日均量×1.3)")
    days_since_ipo: Optional[int] = Field(None, description="上市天数")


class AlphaScore(BaseModel):
    """预期差评分：瓶颈重要性高 + 市场关注度低 = 高 Alpha 潜力。"""

    market_attention: float = Field(default=0.0, ge=0, le=10, description="市场关注度 0-10")
    information_gap: float = Field(default=0.0, ge=0, le=10, description="信息差评分 0-10")
    alpha_score: float = Field(default=0.0, ge=0, le=10, description="综合预期差 0-10")
    trend_bonus: float = Field(default=0.0, description="盈利趋势加分 -1.0~+2.5")
    smart_money_bonus: float = Field(default=0.0, description="聪明钱加分 -1.0~+2.0")
    catalyst_bonus: float = Field(default=0.0, description="催化剂紧迫度加分 0~2.0")
    dim_cap: float = Field(default=5.0, description="市值规模维度得分 0-9")
    dim_analyst: float = Field(default=5.0, description="分析师覆盖维度得分 0-9")
    dim_volume: float = Field(default=5.0, description="成交量动量维度得分 0-9")
    dim_price: float = Field(default=5.0, description="近3月涨幅维度得分 0-9")
    dim_institution: float | None = Field(default=5.0, description="机构持仓维度得分 0-9（A股无数据时为None）")
    ipo_bonus: float = Field(default=0.0, description="IPO加分 (0 or 2)")
    vp_discount: float = Field(default=1.0, description="量价背离折扣系数 (1.0 or 0.8)")
    reasoning: str = ""


class MoatScore(BaseModel):
    """竞争护城河评分。"""

    patent_moat: float = Field(default=0, ge=0, le=10, description="专利/技术壁垒")
    switching_cost: float = Field(default=0, ge=0, le=10, description="客户转换成本")
    capacity_lead_time: float = Field(default=0, ge=0, le=10, description="产能/交期优势")
    cost_advantage: float = Field(default=0, ge=0, le=10, description="成本优势")
    overall_moat: float = Field(default=0, ge=0, le=10, description="护城河综合评分")
    moat_reasoning: str = Field(default="", description="护城河分析要点")


class SmartMoneySignal(BaseModel):
    """聪明钱信号：机构/内部人行为数据。"""

    institution_holding_change: Optional[float] = Field(None, description="机构持仓变动(%)")
    insider_net_shares: Optional[float] = Field(None, description="内部人净买入股数(万股)")
    northbound_net_buy: Optional[float] = Field(None, description="北向资金净买入(万元)")
    margin_balance_change: Optional[float] = Field(None, description="融资余额变化(%)")
    fund_flow_net: Optional[float] = Field(None, description="主力资金净流入(万元)")
    lhb_net_buy: Optional[float] = Field(None, description="龙虎榜机构席位净买入(万元)")
    short_interest_pct: Optional[float] = Field(None, description="做空占流通股比例(%)")
    institution_count: Optional[int] = Field(None, description="持仓机构数量")
    smart_money_score: float = Field(default=5.0, ge=0, le=10, description="聪明钱综合评分 0-10")
    signal_direction: str = Field(default="neutral", description="信号方向: bullish/neutral/bearish")
    details: list[str] = Field(default_factory=list, description="信号明细说明")


class CatalystEvent(BaseModel):
    """单个催化剂事件。"""

    event_type: str = Field(description="催化剂类型: policy/capacity/technology/order/earnings")
    description: str = Field(description="事件描述")
    expected_date: str = Field(default="", description="预期时间 e.g. 2025Q3 或 2025-09")
    confidence: float = Field(default=5.0, ge=0, le=10, description="置信度 0-10")
    impact_score: float = Field(default=5.0, ge=0, le=10, description="影响力 0-10")


class CatalystTimeline(BaseModel):
    """催化剂时间线分析结果。"""

    events: list[CatalystEvent] = Field(default_factory=list, description="催化剂事件列表")
    urgency_score: float = Field(default=5.0, ge=0, le=10, description="紧迫度评分 0-10（越高=越快兑现）")
    investment_window: str = Field(default="", description="建议投资窗口 e.g. '未来1-2个季度'")
    summary: str = Field(default="", description="催化剂一句话总结")


class FinalScore(BaseModel):
    """统一最终评分：quality^w_q × alpha^w_a 几何加权均值。"""

    quality_score: float = Field(ge=0, le=10, description="质量评分（= overall_score）")
    alpha_score: float = Field(ge=0, le=10, description="预期差评分")
    final_score: float = Field(ge=0, le=10, description="最终综合评分")
    quality_weight: float = Field(default=0.55, description="质量权重")
    alpha_weight: float = Field(default=0.45, description="预期差权重")


class SupplierScorecard(BaseModel):
    """Evaluation scorecard for a supplier."""

    supplier: SupplierInfo
    bottleneck_node: str
    layer: int = Field(default=0, description="产业链层级深度")
    market_position: float = Field(ge=0, le=10)
    customer_validation: float = Field(ge=0, le=10)
    capacity_status: float = Field(ge=0, le=10)
    financial_health: float = Field(ge=0, le=10)
    valuation: float = Field(ge=0, le=10)
    overall_score: float = Field(ge=0, le=10)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    financial_snapshot: Optional[FinancialSnapshot] = Field(None, description="真实财务数据快照")
    alpha: Optional[AlphaScore] = Field(None, description="预期差评分")
    moat: Optional[MoatScore] = Field(None, description="竞争护城河评分")
    smart_money: Optional[SmartMoneySignal] = Field(None, description="聪明钱信号")
    catalyst: Optional[CatalystTimeline] = Field(None, description="催化剂时间线")
    final: Optional[FinalScore] = Field(None, description="统一最终评分")

    @model_serializer(mode="wrap")
    def _serialize_with_dimension_scores(self, handler):
        d = handler(self)
        d["dimension_scores"] = {
            "position": self.market_position,
            "customer": self.customer_validation,
            "capacity": self.capacity_status,
            "financial": self.financial_health,
            "valuation": self.valuation,
        }
        return d


class FinalScoredCompany(BaseModel):
    """Phase 3 输出：带最终评分排名的公司。"""

    rank: int = Field(ge=1, description="最终排名")
    scorecard: SupplierScorecard
    final: FinalScore
    key_factors: list[str] = Field(default_factory=list, description="关键决策因子")


class ValidationResult(str, Enum):
    PASS = "pass"
    CONCERN = "concern"
    FAIL = "fail"


class ModelValidation(BaseModel):
    """One model's cross-validation result for a supplier."""

    model_name: str
    score: float = Field(ge=1, le=10, description="推荐评分 1-10")
    reasoning: str
    concerns: list[str] = Field(default_factory=list)
    perspective: str = Field(default="", description="验证视角: financial/chain/sentiment/blind")
    fatal_risk: bool = Field(default=False, description="是否触发致命风险")
    fatal_reason: str = Field(default="", description="致命风险原因")
    weight: float = Field(default=1.0, description="校准权重")


class CrossValidationReport(BaseModel):
    """Multi-model cross-validation result for a supplier."""

    supplier_name: str
    ticker: str
    validations: list[ModelValidation]
    consensus_score: float = Field(ge=0, le=10, description="多模型共识评分（加权）")
    consensus_reasoning: str
    avg_score: float = Field(ge=0, le=10, description="原始均分")
    raw_avg: float = Field(default=0.0, description="原始均分（未去极值）")
    trimmed_avg: float = Field(default=0.0, description="去极值均分")
    has_fatal_risk: bool = Field(default=False, description="是否触发一票否决")
    fatal_risks: list[str] = Field(default_factory=list, description="致命风险列表")
    outlier_challenges: list[dict] = Field(default_factory=list, description="离群值追问记录")


class ScreeningResult(BaseModel):
    """Final screening output for a sector."""

    sector: str
    chain: ChainGraph
    bottleneck_reports: list[BottleneckReport]
    supplier_scorecards: list[SupplierScorecard]
    cross_validations: list[CrossValidationReport]
    top_picks: list[str] = Field(default_factory=list, description="Ticker symbols of final recommendations")


# ── AI 投研圆桌会议 ──────────────────────────────────────────

class MeetingMessage(BaseModel):
    """圆桌会议中的一条发言。"""

    round_num: int = Field(description="0=开场, 1=独立提名, 2=辩论, 3=总结")
    role: str = Field(description="growth/value/risk/chain/host")
    participant_name: str
    model_name: str = ""
    content: str = Field(description="展示用自然语言")
    structured_data: dict | None = Field(None, description="LLM 返回的原始 JSON")


class MeetingRanking(BaseModel):
    """圆桌会议最终排名中的一条。"""

    rank: int
    ticker: str
    name: str
    borda_points: int = 0
    weighted_score: float = Field(default=0.0, description="加权信心分（0-100）")
    supporter_count: int = 0
    supporters: list[str] = Field(default_factory=list, description="投票支持的角色 ID")
    opposers: list[str] = Field(default_factory=list, description="未投票的角色 ID")
    reasoning: str = ""


class RoundtableMeetingResult(BaseModel):
    """圆桌会议完整结果。"""

    participants: list[dict] = Field(default_factory=list)
    transcript: list[MeetingMessage] = Field(default_factory=list)
    final_ranking: list[MeetingRanking] = Field(default_factory=list)
    key_agreements: list[str] = Field(default_factory=list)
    key_disagreements: list[str] = Field(default_factory=list)
    risk_warnings: list[str] = Field(default_factory=list)
    investment_thesis: str = ""
