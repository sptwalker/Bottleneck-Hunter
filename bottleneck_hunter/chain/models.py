"""Data models for industry chain analysis."""

from __future__ import annotations

import calendar
import logging
import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_serializer

logger = logging.getLogger(__name__)


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
    # ── 约束类字段（P2-1）─────────────────────────────────────────────
    # 这五项是「这个环节会不会被卡脖子」的直接判据，且**全部由拆解阶段可知** ——
    # 丢掉之后，下游分析层拿不到任何结构性事实，只能重新从 0 猜。
    #
    # 订正：审查称它们「此前靠 LLM 在 `notes` 自由文本里带一句」——**`IndustryNode`
    # 没有 `notes` 字段**（`ChainLink` 才有）。生产实测：8484 个节点，五项字段
    # 一个都没有（0/8484）。所以不是「有位置放但不结构化」，是**根本没地方放**。
    # 自由文本那条通路确实存在，但在 link 上，见 `bottleneck._build_context`
    # 现在把两处都接进了打分 prompt。
    #
    # 缺失一律用 None（= 未采集），**不用默认值冒充**：0 个月扩产周期和「不知道」
    # 是完全不同的结论，正如 `AlphaScore` 不用 5.0 表示无数据。
    supply_structure: str | None = Field(
        default=None,
        description="供应结构: single(独家) / oligopoly(寡头 2-3 家) / multi(多家竞争) / unknown",
    )
    capacity_lead_time_months: int | None = Field(
        default=None, ge=0, description="产能扩张周期（月）—— 供需缺口能持续多久"
    )
    qualification_cycle_months: int | None = Field(
        default=None, ge=0, description="新进入者认证周期（月）—— 半导体/医药/航空的核心壁垒"
    )
    geo_concentration: str | None = Field(
        default=None, description="地理集中度，如「日本 90%」—— 单一地域集中的尾部风险"
    )
    export_control_risk: str | None = Field(
        default=None, description="出口管制/政策风险: high / medium / low / unknown"
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

    def get_node(self, name: str) -> IndustryNode | None:
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
    rank: int | None = None
    key_insights: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    cr3_estimate: int | None = Field(None, ge=0, le=100, description="CR3 市场集中度(%)，来源见 cr3_source")
    hhi_estimate: int | None = Field(None, ge=0, le=10000, description="HHI 赫芬达尔指数，来源见 cr3_source")
    hhi_adjustments: list[str] = Field(default_factory=list, description="HHI 一致性校验的调整记录")
    # 集中度数据来源标注：区分真实计算 vs LLM 估算，供前端徽章与可信度判断
    cr3_source: str = Field("llm_estimate", description="'akshare'=板块成分股真实计算 | 'llm_estimate'=LLM估算")
    concentration_detail: dict | None = Field(None, description="真实集中度明细(板块名/公司数/CR5/Top公司)，仅 akshare 来源时有")
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
    market_cap: float | None = Field(None, description="Market cap in local currency (亿 for A-stock, $B for US)")
    sector: str
    description: str
    market_share: float | None = Field(None, description="Market share percentage if known")
    key_products: list[str] = Field(default_factory=list)
    revenue_growth: float | None = None
    gross_margin: float | None = None
    pe_ratio: float | None = None
    institution_holding_pct: float | None = None
    source: str = Field(default="llm", description="候选来源: llm / akshare / chain")
    sources: list[str] = Field(
        default_factory=list,
        description="该票被哪些源命中过（多源留痕，首源为准但其余源的字段会回填空缺）",
    )


class QuarterlyDataPoint(BaseModel):
    """单季度财务数据点。"""

    report_date: str = Field(default="", description="报告期 e.g. 2025-03-31")
    revenue_yi: float | None = Field(None, description="营业总收入(亿)")
    net_profit_yi: float | None = Field(None, description="归母净利润(亿)")
    gross_margin_pct: float | None = Field(None, description="销售毛利率(%)")
    roe_pct: float | None = Field(None, description="净资产收益率(%)")
    revenue_yoy_pct: float | None = Field(None, description="营收同比增速(%)")
    net_profit_yoy_pct: float | None = Field(None, description="净利润同比增速(%)")


class FinancialTrend(BaseModel):
    """多季度财务趋势分析结果。"""

    quarters: list[QuarterlyDataPoint] = Field(default_factory=list, description="近N个季度数据，按时间降序")
    revenue_acceleration: float | None = Field(None, description="营收加速度: 最近2Q平均增速 - 前2Q平均增速")
    gross_margin_trend: float | None = Field(None, description="毛利率趋势: 最近2Q均值 - 前2Q均值（百分点）")
    consecutive_growth_quarters: int = Field(default=0, description="连续营收正增长季度数")
    profit_acceleration: float | None = Field(None, description="净利润加速度: 最近2Q平均增速 - 前2Q平均增速")
    trend_summary: str = Field(default="", description="趋势一句话摘要")


class FinancialSnapshot(BaseModel):
    """真实财务数据快照，来自市场 API 而非 LLM。"""

    data_source: str = Field(default="", description="akshare_ths / yfinance / tencent")
    report_date: str = Field(default="", description="最近一期财报日期 e.g. 2025-12-31")

    revenue_yi: float | None = Field(None, description="营业总收入(亿)")
    revenue_yoy_pct: float | None = Field(None, description="营收同比增速(%)")
    net_profit_yi: float | None = Field(None, description="归母净利润(亿)")
    net_profit_yoy_pct: float | None = Field(None, description="净利润同比增速(%)")
    gross_margin_pct: float | None = Field(None, description="销售毛利率(%)")
    roe_pct: float | None = Field(None, description="净资产收益率(%)")
    debt_ratio_pct: float | None = Field(None, description="资产负债率(%)")
    cashflow_per_share: float | None = Field(None, description="每股经营现金流")

    main_business: dict | None = Field(None, description="主营构成（分产品营收/占比/毛利率，仅A股 Gangtise）")
    analyst_report_count: int | None = Field(None, description="近期研报覆盖数")
    analyst_rating: str | None = Field(None, description="最新机构评级")
    consensus_eps: float | None = Field(None, description="一致预期 EPS（当年）")
    consensus_pe: float | None = Field(None, description="一致预期 PE（当年）")

    trend: FinancialTrend | None = Field(None, description="多季度财务趋势")

    volume_ratio: float | None = Field(None, description="成交量动量: 过滤后10日均量/60日均量")
    price_change_3m_pct: float | None = Field(None, description="近3月涨幅(%)")
    price_change_1m_pct: float | None = Field(None, description="近1月涨幅(%)")
    institution_holding_pct: float | None = Field(None, description="机构持仓占流通股比例(%)")
    consecutive_volume_days: int = Field(default=0, description="连续放量天数(日成交量>60日均量×1.3)")
    days_since_ipo: int | None = Field(None, description="上市天数")
    avg_daily_amount_wan: float | None = Field(None, description="日均成交额(万，本币；A股万元/美股万美元)")

    @field_validator("debt_ratio_pct")
    @classmethod
    def _validate_debt_ratio(cls, v: float | None) -> float | None:
        """值域守卫：本字段是资产负债率 D/A(%)，合理区间 0-100。

        越界即说明上游填错了字段（历史上美股路径误塞 D/E 百分数进来），
        宁可丢这个数也不能让错口径流进评分与事实核查。
        """
        if v is None:
            return None
        if not 0.0 <= v <= 100.0:
            logger.warning("debt_ratio_pct 越界(%.4f)，已丢弃 —— 上游可能填了 D/E 而非 D/A", v)
            return None
        return v

    @field_validator("cashflow_per_share")
    @classmethod
    def _validate_cfps(cls, v: float | None) -> float | None:
        """值域守卫：本字段是**每股**经营现金流，不是总额。

        总量级（>1e6）只可能来自未除股本的 operatingCashflow 总额，丢弃并告警。
        """
        if v is None:
            return None
        if abs(v) > 1e6:
            logger.warning("cashflow_per_share 量级异常(%.4g)，已丢弃 —— 上游可能存了现金流总额", v)
            return None
        return v


class AlphaScore(BaseModel):
    """预期差评分：瓶颈重要性高 + 市场关注度低 = 高 Alpha 潜力。

    market_attention / information_gap / alpha_score 在全维度无数据时为 None。
    下游消费方须做 None 守卫，不应回退到中性分 5.0（None = 数据不足，5.0 = 中等关注度，语义不同）。
    """

    market_attention: float | None = Field(default=None, ge=0, le=10, description="市场关注度 0-10；无数据时为 None")
    information_gap: float | None = Field(default=None, ge=0, le=10, description="信息差评分 0-10；无数据时为 None")
    alpha_score: float | None = Field(default=None, ge=0, le=10, description="综合预期差 0-10；无数据时为 None")
    trend_bonus: float = Field(default=0.0, description="盈利趋势加分 -1.0~+2.5")
    smart_money_bonus: float = Field(default=0.0, description="聪明钱加分 -1.0~+2.0")
    catalyst_bonus: float = Field(default=0.0, description="催化剂紧迫度加分 0~2.0")
    dim_cap: float | None = Field(default=None, description="市值规模维度得分 0-9；无数据时为 None")
    dim_analyst: float | None = Field(default=None, description="分析师覆盖维度得分 0-9；无数据时为 None")
    dim_volume: float | None = Field(default=None, description="成交量动量维度得分 0-9；无数据时为 None")
    dim_price: float | None = Field(default=None, description="近3月涨幅维度得分 0-9；无数据时为 None")
    dim_institution: float | None = Field(default=None, description="机构持仓维度得分 0-9（A股或无数据时为 None）")
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

    institution_holding_change: float | None = Field(None, description="机构持仓变动(%)")
    insider_net_shares: float | None = Field(None, description="内部人净买入股数(万股)")
    northbound_net_buy: float | None = Field(None, description="北向资金净买入(万元)")
    margin_balance_change: float | None = Field(None, description="融资余额变化(%)")
    fund_flow_net: float | None = Field(None, description="主力资金净流入(万元)")
    lhb_net_buy: float | None = Field(None, description="龙虎榜机构席位净买入(万元)")
    short_interest_pct: float | None = Field(None, description="做空占流通股比例(%)")
    institution_count: int | None = Field(None, description="持仓机构数量")
    smart_money_score: float = Field(default=5.0, ge=0, le=10, description="聪明钱综合评分 0-10")
    signal_direction: str = Field(default="neutral", description="信号方向: bullish/neutral/bearish")
    details: list[str] = Field(default_factory=list, description="信号明细说明")


# 模糊期间 → 该期间的最后一天。「预期在某季度兑现」用期末表示：到期日算得越晚，
# 越不会把还没到期的催化剂误判成「已过期」，方向是保守的。
_PERIOD_END = {
    "Q1": (3, 31), "Q2": (6, 30), "Q3": (9, 30), "Q4": (12, 31),
    "H1": (6, 30), "H2": (12, 31),
    "上半年": (6, 30), "下半年": (12, 31),
}
_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

# `2025Q3-Q4` / `2025H2-H1` 这类简写里，后半段的年份被省略了。先补全再解析，
# 否则 `Q4` 因无年份被整段丢弃，区间被当成单点 Q3 —— 到期日凭空提前一个季度。
# 中英混写的 `2025年Q3-Q4` 同样要吃进来，否则同一个 bug 会换个写法复发。
_PERIOD_TOKEN = re.compile(r"(\d{4})\s*年?\s*([QHqh])\s*([1-4])|([QHqh])\s*([1-4])")


def _expand_bare_periods(s: str) -> str:
    """给省略年份的期间记号补上前面出现过的年份。"""
    last_year = ""

    def _sub(m: re.Match) -> str:
        nonlocal last_year
        if m.group(1):
            last_year = m.group(1)
            return m.group(0)
        return f"{last_year}{m.group(4)}{m.group(5)}" if last_year else m.group(0)

    return _PERIOD_TOKEN.sub(_sub, s)


def _normalize_expected_date(raw) -> str:
    """把 LLM 写的模糊时间归一成 YYYY-MM-DD；无从解析则返回 ""（= 无日期，不臆测）。

    实测生产库里 `2025Q3`(338) / `2025Q4`(272) / `2025H2` / `2025年下半年` / `2025-08`
    这类写法占了绝大多数，而所有下游（`_days_until_date`、`_date_diff`、各种 [:10]
    切片）都只认 ISO —— 于是它们**静默解析失败**，等同于「这家没有催化剂」。
    同类写法按期末折算后取**最晚**的一个（`2025Q4-2026Q1` → 2026-03-31），
    与「6-18 个月内」的时间窗语义一致。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    s = _expand_bare_periods(s)
    candidates: list[str] = []
    for m in re.finditer(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):          # 2025-09-30
        y, mo, d = (int(g) for g in m.groups())
        if 1 <= mo <= 12 and 1 <= d <= _last_day(y, mo):
            candidates.append(f"{y:04d}-{mo:02d}-{d:02d}")
    for m in re.finditer(r"(\d{4})-(\d{1,2})(?![\d-])", s):            # 2025-08（月末）
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            candidates.append(f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}")
    for m in re.finditer(r"(\d{4})\s*年?\s*[Qq]([1-4])", s):           # 2025Q3 / 2025年Q3
        candidates.append(_period_end(int(m.group(1)), f"Q{m.group(2)}"))
    for m in re.finditer(r"(\d{4})\s*年?\s*[Hh]([12])", s):            # 2025H2 / 2025年H2
        candidates.append(_period_end(int(m.group(1)), f"H{m.group(2)}"))
    for m in re.finditer(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s):          # 2025年5月
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            candidates.append(f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}")
    for m in re.finditer(r"(\d{4})\s*年\s*(\d{1,2})\s*月?\s*[-~至到]\s*(\d{1,2})\s*月", s):  # 2025年7-8月 / 7月至8月
        y, mo = int(m.group(1)), int(m.group(3))                        # 取后一个月（保守方向）
        if 1 <= mo <= 12:
            candidates.append(f"{y:04d}-{mo:02d}-{_last_day(y, mo):02d}")
    for m in re.finditer(r"(\d{4})\s*年\s*(上半年|下半年)", s):          # 2025年下半年
        candidates.append(_period_end(int(m.group(1)), m.group(2)))
    for m in re.finditer(r"(\d{4})\s*年?\s*第?\s*([1-4一二三四])\s*季度", s):  # 2025年第3季度
        q = _CN_QUARTER.get(m.group(2))
        if q:
            candidates.append(_period_end(int(m.group(1)), f"Q{q}"))
    # 整年写法（末位兜底）：`2025全年` / `2025年内` / `2025年` / `2025-2026年`。
    # 只在上面的精确写法**一个都没匹配到**时才启用 —— 否则 `2025Q3` 会多出
    # 一个 2025-12-31 的候选，被 `max` 选中，季度末语义被整年末盖掉。
    # 末尾断言排除「2026万元」这类**数量**（4 位数跟着单位），否则金额会被读成日期。
    if not any(candidates):
        for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)\s*年?(?![万亿个%元股])", s):
            y = int(m.group(1))
            if 1990 <= y <= 2100:
                candidates.append(f"{y:04d}-12-31")
    if not candidates:
        logger.debug("expected_date 无法解析为日期: %r", raw)
        return ""
    return max(c for c in candidates if c)


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _period_end(year: int, period: str) -> str:
    """模糊期间 → 期末 ISO 日期；未知期间返回 ""。"""
    mmdd = _PERIOD_END.get(period.upper() if period.isascii() else period)
    if not mmdd:
        return ""
    return f"{year:04d}-{mmdd[0]:02d}-{mmdd[1]:02d}"


class CatalystEvent(BaseModel):
    """单个催化剂事件。"""

    event_type: str = Field(description="催化剂类型: policy/capacity/technology/order/earnings")
    description: str = Field(description="事件描述")
    expected_date: str = Field(default="", description="预期日期 YYYY-MM-DD（写入时由 validator 归一）")
    confidence: float = Field(default=5.0, ge=0, le=10, description="置信度 0-10")
    impact_score: float = Field(default=5.0, ge=0, le=10, description="影响力 0-10")

    @field_validator("expected_date", mode="before")
    @classmethod
    def _normalize_date(cls, v):
        return _normalize_expected_date(v)


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
    credibility: float | None = Field(None, ge=0, le=10, description="事实核查可信度(FactCheck)")
    quality_adjusted: float | None = Field(None, ge=0, le=10, description="credibility调整后的quality")


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
    financial_snapshot: FinancialSnapshot | None = Field(None, description="真实财务数据快照")
    alpha: AlphaScore | None = Field(None, description="预期差评分")
    moat: MoatScore | None = Field(None, description="竞争护城河评分")
    smart_money: SmartMoneySignal | None = Field(None, description="聪明钱信号")
    catalyst: CatalystTimeline | None = Field(None, description="催化剂时间线")
    final: FinalScore | None = Field(None, description="统一最终评分")
    fact_check_recommendation: str | None = Field(None, description="事实核查建议: PASS/REVIEW/REJECT")
    # 数据覆盖度（P1-11）：customer_validation / capacity_status 是唯二无数据锚的维度，
    # 数据缺失时它们的权重占比反而从 26.7% 升到 40% —— "缺数据"被算成了"更依赖 LLM"。
    # 这两个字段把这件事透出来，让下游知道结论有多可靠，而不是看起来一样地自信。
    data_coverage: float | None = Field(None, ge=0, le=1, description="有真实数据锚的维度权重占比")
    llm_only_dims: list[str] = Field(default_factory=list, description="纯 LLM 给分、无数据锚的维度名")

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
