"""Hot sector and theme rotation detector.

Uses East Money APIs via AKShare to automatically detect:
- Current hot sectors by capital flow (资金流向)
- Sector price change rankings (涨幅排名)
- Emerging theme rotations (板块轮动)

Outputs ranked sector suggestions that can be fed into the chain analysis pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class HotSector:
    """A single hot sector with multi-signal scoring."""

    name: str
    sector_type: str  # "concept" | "industry"
    price_change_pct: float | None = None  # 涨跌幅%
    turnover_rate: float | None = None  # 换手率%
    main_net_inflow: float | None = None  # 主力净流入(亿)
    volume: float | None = None  # 成交额(亿)
    up_count: int | None = None  # 上涨家数
    down_count: int | None = None  # 下跌家数
    leader_stock: str | None = None  # 领涨股
    composite_score: float = 0.0  # 综合热度评分
    signal_count: int = 0  # 命中信号数


@dataclass
class HotSectorResult:
    """Detection result with ranked hot sectors."""

    concept_sectors: list[HotSector] = field(default_factory=list)
    industry_sectors: list[HotSector] = field(default_factory=list)
    all_ranked: list[HotSector] = field(default_factory=list)
    emerging_themes: list[HotSector] = field(default_factory=list)  # 轮动信号


def _safe_float(val) -> float | None:
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None or pd.isna(val):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class HotSectorDetector:
    """Detect hot sectors from East Money data.

    Combines multiple signals:
    1. Price change ranking (涨幅排名)
    2. Capital flow ranking (资金流入排名)
    3. Turnover rate (换手率异常)
    4. Breadth (上涨家数 vs 下跌家数)
    """

    def __init__(
        self,
        top_n: int = 20,
        min_price_change: float = 1.0,
        min_turnover: float = 2.0,
    ):
        """
        Args:
            top_n: How many top sectors to return per signal.
            min_price_change: Minimum price change % to consider "hot".
            min_turnover: Minimum turnover rate % to consider "active".
        """
        self.top_n = top_n
        self.min_price_change = min_price_change
        self.min_turnover = min_turnover

    def detect(self) -> HotSectorResult:
        """Run full hot sector detection pipeline."""
        # Collect data from multiple sources
        concept_price = self._fetch_concept_price_ranking()
        industry_price = self._fetch_industry_price_ranking()
        concept_flow = self._fetch_concept_capital_flow()
        industry_flow = self._fetch_industry_capital_flow()

        # Merge and score
        concept_sectors = self._merge_and_score(
            concept_price, concept_flow, "concept"
        )
        industry_sectors = self._merge_and_score(
            industry_price, industry_flow, "industry"
        )

        # Combine and rank
        all_sectors = concept_sectors + industry_sectors
        all_sectors.sort(key=lambda s: s.composite_score, reverse=True)

        # Detect rotation signals
        emerging = self._detect_rotation(all_sectors)

        return HotSectorResult(
            concept_sectors=concept_sectors[: self.top_n],
            industry_sectors=industry_sectors[: self.top_n],
            all_ranked=all_sectors[: self.top_n],
            emerging_themes=emerging,
        )

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_concept_price_ranking(self) -> pd.DataFrame:
        """Fetch concept board price change ranking from East Money."""
        try:
            df = ak.stock_board_concept_name_em()
            # Sort by 涨跌幅 descending
            if "涨跌幅" in df.columns:
                df = df.sort_values("涨跌幅", ascending=False)
            logger.info(f"Fetched {len(df)} concept boards (price ranking)")
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch concept price ranking: {e}")
            return pd.DataFrame()

    def _fetch_industry_price_ranking(self) -> pd.DataFrame:
        """Fetch industry board price change ranking from East Money."""
        try:
            df = ak.stock_board_industry_name_em()
            if "涨跌幅" in df.columns:
                df = df.sort_values("涨跌幅", ascending=False)
            logger.info(f"Fetched {len(df)} industry boards (price ranking)")
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch industry price ranking: {e}")
            return pd.DataFrame()

    def _fetch_concept_capital_flow(self) -> pd.DataFrame:
        """Fetch concept sector capital flow ranking."""
        try:
            df = ak.stock_sector_fund_flow_rank(
                indicator="今日", sector_type="概念资金流"
            )
            logger.info(f"Fetched {len(df)} concept capital flow rows")
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch concept capital flow: {e}")
            return pd.DataFrame()

    def _fetch_industry_capital_flow(self) -> pd.DataFrame:
        """Fetch industry sector capital flow ranking."""
        try:
            df = ak.stock_sector_fund_flow_rank(
                indicator="今日", sector_type="行业资金流"
            )
            logger.info(f"Fetched {len(df)} industry capital flow rows")
            return df
        except Exception as e:
            logger.warning(f"Failed to fetch industry capital flow: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # Merging & scoring
    # ------------------------------------------------------------------

    def _merge_and_score(
        self,
        price_df: pd.DataFrame,
        flow_df: pd.DataFrame,
        sector_type: str,
    ) -> list[HotSector]:
        """Merge price ranking and capital flow data, compute composite scores."""
        sectors: dict[str, HotSector] = {}

        # Process price ranking
        for _, row in price_df.head(self.top_n * 3).iterrows():
            name = str(row.get("板块名称", ""))
            if not name:
                continue
            change = _safe_float(row.get("涨跌幅"))
            turnover = _safe_float(row.get("换手率"))
            volume = _safe_float(row.get("成交额"))
            up = _safe_int(row.get("上涨家数"))
            down = _safe_int(row.get("下跌家数"))
            leader = str(row.get("领涨股票", row.get("领涨股", "")))

            sectors[name] = HotSector(
                name=name,
                sector_type=sector_type,
                price_change_pct=change,
                turnover_rate=turnover,
                volume=volume,
                up_count=up,
                down_count=down,
                leader_stock=leader if leader and leader != "nan" else None,
            )

        # Merge capital flow data
        for _, row in flow_df.head(self.top_n * 3).iterrows():
            name = str(row.get("板块名称", ""))
            if not name:
                continue
            flow_val = _safe_float(row.get("主力净流入", row.get("今日主力净流入-净额", None)))

            if name in sectors:
                sectors[name].main_net_inflow = flow_val
            else:
                sectors[name] = HotSector(
                    name=name,
                    sector_type=sector_type,
                    main_net_inflow=flow_val,
                )

        # Score each sector
        for s in sectors.values():
            self._compute_score(s)

        return sorted(sectors.values(), key=lambda x: x.composite_score, reverse=True)

    def _compute_score(self, sector: HotSector) -> None:
        """Compute composite hotness score for a sector.

        Scoring logic:
        - Price change > threshold: +3 points per %
        - Main capital inflow > 0: +5 points per 亿
        - Turnover rate > threshold: +1 point per %
        - Breadth (up > down): +2
        - Multi-signal bonus: +2 for each additional signal present
        """
        score = 0.0
        signals = 0

        # Signal 1: Price change
        if sector.price_change_pct is not None and sector.price_change_pct > self.min_price_change:
            score += min(sector.price_change_pct * 0.3, 10)
            signals += 1

        # Signal 2: Capital inflow
        if sector.main_net_inflow is not None and sector.main_net_inflow > 0:
            score += min(sector.main_net_inflow * 0.5, 10)
            signals += 1

        # Signal 3: Turnover rate
        if sector.turnover_rate is not None and sector.turnover_rate > self.min_turnover:
            score += min(sector.turnover_rate * 0.2, 5)
            signals += 1

        # Signal 4: Breadth
        if sector.up_count is not None and sector.down_count is not None:
            if sector.up_count > sector.down_count * 2:
                score += 2
                signals += 1

        # Multi-signal bonus
        if signals >= 3:
            score += 3
        elif signals >= 2:
            score += 1

        sector.composite_score = round(score, 1)
        sector.signal_count = signals

    # ------------------------------------------------------------------
    # Rotation detection
    # ------------------------------------------------------------------

    def _detect_rotation(self, all_sectors: list[HotSector]) -> list[HotSector]:
        """Identify emerging theme rotation signals.

        A sector is considered "emerging" if:
        - It has a high composite score (hot)
        - It's a concept board (thematic, not industry)
        - It has strong capital inflow (institutional interest)
        """
        emerging = []
        for s in all_sectors:
            if s.sector_type != "concept":
                continue
            if s.composite_score < 5.0:
                continue
            # Must have at least 2 signals
            if s.signal_count < 2:
                continue
            emerging.append(s)

        return emerging[:10]

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_result(self, result: HotSectorResult, language: str = "zh") -> str:
        """Format detection result as readable text."""
        lines: list[str] = []

        if language == "zh":
            lines.append("## 当前热点板块检测\n")

            # Top hot sectors
            if result.all_ranked:
                lines.append("### 综合热度排名 Top 10\n")
                lines.append("| 排名 | 板块 | 类型 | 涨幅% | 资金流入(亿) | 换手率% | 信号数 | 热度分 |")
                lines.append("|------|------|------|-------|-------------|---------|--------|--------|")
                for i, s in enumerate(result.all_ranked[:10], 1):
                    change_str = f"{s.price_change_pct:.2f}" if s.price_change_pct is not None else "-"
                    flow_str = f"{s.main_net_inflow:.2f}" if s.main_net_inflow is not None else "-"
                    turnover_str = f"{s.turnover_rate:.2f}" if s.turnover_rate is not None else "-"
                    type_str = "概念" if s.sector_type == "concept" else "行业"
                    lines.append(
                        f"| {i} | {s.name} | {type_str} "
                        f"| {change_str} | {flow_str} | {turnover_str} "
                        f"| {s.signal_count} | **{s.composite_score:.1f}** |"
                    )
                lines.append("")

            # Emerging themes
            if result.emerging_themes:
                lines.append("### 新兴题材轮动信号\n")
                for s in result.emerging_themes[:5]:
                    lines.append(f"- **{s.name}** (热度 {s.composite_score:.1f})")
                    if s.price_change_pct is not None:
                        lines.append(f"  - 涨幅: {s.price_change_pct:.2f}%")
                    if s.main_net_inflow is not None:
                        lines.append(f"  - 主力资金: {s.main_net_inflow:.2f}亿")
                    if s.leader_stock:
                        lines.append(f"  - 领涨股: {s.leader_stock}")
                lines.append("")

            # Suggested analysis targets
            lines.append("### 建议分析方向\n")
            for s in result.emerging_themes[:5]:
                lines.append(f"- `{s.name}` — 可作为产业链选股的输入方向")
            lines.append("")

        return "\n".join(lines)


def detect_hot_sectors(top_n: int = 20) -> HotSectorResult:
    """Convenience function to run hot sector detection."""
    detector = HotSectorDetector(top_n=top_n)
    return detector.detect()
