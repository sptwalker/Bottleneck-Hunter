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


class AlphaScore(BaseModel):
    """预期差评分：瓶颈重要性高 + 市场关注度低 = 高 Alpha 潜力。"""

    market_attention: float = Field(default=0.0, ge=0, le=10, description="市场关注度 0-10")
    information_gap: float = Field(default=0.0, ge=0, le=10, description="信息差评分 0-10")
    alpha_score: float = Field(default=0.0, ge=0, le=10, description="综合预期差 0-10")
    reasoning: str = ""


class SupplierScorecard(BaseModel):
    """Evaluation scorecard for a supplier."""

    supplier: SupplierInfo
    bottleneck_node: str
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


class CrossValidationReport(BaseModel):
    """Multi-model cross-validation result for a supplier."""

    supplier_name: str
    ticker: str
    validations: list[ModelValidation]
    consensus_score: float = Field(ge=1, le=10, description="多模型共识评分（平均值）")
    consensus_reasoning: str
    avg_score: float = Field(ge=1, le=10, description="平均评分")


class ScreeningResult(BaseModel):
    """Final screening output for a sector."""

    sector: str
    chain: ChainGraph
    bottleneck_reports: list[BottleneckReport]
    supplier_scorecards: list[SupplierScorecard]
    cross_validations: list[CrossValidationReport]
    top_picks: list[str] = Field(default_factory=list, description="Ticker symbols of final recommendations")
