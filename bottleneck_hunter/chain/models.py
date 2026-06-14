"""Data models for industry chain analysis."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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

    def model_dump(self, **kwargs) -> dict:
        d = super().model_dump(**kwargs)
        # 前端 dashboard.js 需要 dimension_scores 嵌套字典
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
    result: ValidationResult
    reasoning: str
    concerns: list[str] = Field(default_factory=list)


class CrossValidationReport(BaseModel):
    """Multi-model cross-validation result for a supplier."""

    supplier_name: str
    ticker: str
    validations: list[ModelValidation]
    consensus: ValidationResult
    consensus_reasoning: str
    pass_rate: float = Field(ge=0, le=1, description="Fraction of models that passed")


class ScreeningResult(BaseModel):
    """Final screening output for a sector."""

    sector: str
    chain: ChainGraph
    bottleneck_reports: list[BottleneckReport]
    supplier_scorecards: list[SupplierScorecard]
    cross_validations: list[CrossValidationReport]
    top_picks: list[str] = Field(default_factory=list, description="Ticker symbols of final recommendations")
