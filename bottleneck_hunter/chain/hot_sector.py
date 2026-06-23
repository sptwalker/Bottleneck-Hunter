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
class SectorRecommendation:
    """推荐赛道：将热门板块映射为产业方向+终端产品。"""

    sector: str           # 产业方向
    end_product: str      # 终端产品
    reason: str           # 推荐理由（如"涨幅+3.2%, 主力净流入8.5亿"）
    score: float          # 综合热度分
    market: str = "a_stock"
    source_board: str = ""  # 原始板块名


# 常见 A 股概念板块名 → (产业方向, 终端产品) 映射
BOARD_TO_SECTOR: dict[str, tuple[str, str]] = {
    # AI / 半导体
    "算力": ("GPU/AI算力", "GPU"),
    "芯片": ("半导体芯片", "芯片"),
    "光模块": ("光通信", "光模块"),
    "CPO": ("光通信", "CPO光模块"),
    "存储": ("存储芯片", "DRAM/NAND"),
    "AI": ("人工智能", "AI大模型"),
    "人工智能": ("人工智能", "AI大模型"),
    "大模型": ("人工智能", "AI大模型"),
    "AIGC": ("人工智能", "AIGC应用"),
    "数据中心": ("数据中心", "服务器"),
    "服务器": ("数据中心", "AI服务器"),
    "PCB": ("PCB印制电路板", "高端PCB"),
    "封装": ("先进封装", "Chiplet封装"),
    "EDA": ("EDA/IP", "EDA工具"),
    "光刻": ("半导体设备", "光刻机"),
    # 机器人 / 智能制造
    "机器人": ("人形机器人", "人形机器人"),
    "人形机器人": ("人形机器人", "人形机器人"),
    "减速器": ("精密传动", "RV减速器"),
    "传感器": ("传感器", "智能传感器"),
    # 汽车
    "无人驾驶": ("智能驾驶", "自动驾驶系统"),
    "智能驾驶": ("智能驾驶", "自动驾驶系统"),
    "新能源汽车": ("新能源车", "电动汽车"),
    "新能源车": ("新能源车", "电动汽车"),
    "汽车零部件": ("汽车零部件", "汽车零部件"),
    "充电桩": ("充电基础设施", "充电桩"),
    # 新能源
    "锂电池": ("动力电池", "锂电池"),
    "电池": ("动力电池", "锂电池"),
    "固态电池": ("固态电池", "固态电池"),
    "钠电池": ("钠离子电池", "钠离子电池"),
    "光伏": ("光伏", "光伏组件"),
    "风电": ("风电", "风力发电机"),
    "储能": ("储能", "储能系统"),
    "氢能": ("氢能", "燃料电池"),
    # 军工 / 航天
    "军工": ("国防军工", "武器装备"),
    "航天": ("商业航天", "商业运载火箭"),
    "低空经济": ("低空经济", "eVTOL飞行器"),
    "卫星": ("卫星互联网", "通信卫星"),
    "北斗": ("北斗导航", "北斗芯片"),
    "无人机": ("无人机", "工业无人机"),
    # 医药
    "医药": ("创新药", "创新药物"),
    "创新药": ("创新药", "创新药物"),
    "中药": ("中医药", "中药"),
    "医疗器械": ("医疗器械", "高端医疗设备"),
    "CXO": ("CXO", "医药外包服务"),
    "减肥药": ("减肥药", "GLP-1药物"),
    # 半导体
    "半导体": ("半导体", "半导体设备"),
    # 消费电子
    "消费电子": ("消费电子", "智能手机"),
    "面板": ("显示面板", "OLED面板"),
    "MR": ("混合现实", "MR头显"),
    "VR": ("虚拟现实", "VR设备"),
    # 产业链
    "华为": ("华为产业链", "鸿蒙生态"),
    "鸿蒙": ("华为产业链", "鸿蒙生态"),
    "苹果": ("苹果产业链", "iPhone"),
    "特斯拉": ("特斯拉产业链", "电动汽车"),
    # 其他热门
    "信创": ("信创", "国产替代软硬件"),
    "网络安全": ("网络安全", "安全产品"),
    "云计算": ("云计算", "云服务"),
    "游戏": ("游戏", "网络游戏"),
    "影视": ("影视传媒", "影视内容"),
    "短剧": ("短剧", "短剧内容"),
    "白酒": ("白酒", "高端白酒"),
    "食品": ("食品饮料", "食品"),
    "家电": ("家电", "智能家电"),
    "房地产": ("房地产", "住宅地产"),
    "银行": ("银行", "商业银行"),
    "保险": ("保险", "保险服务"),
    "证券": ("证券", "证券经纪"),
    "稀土": ("稀土", "稀土永磁材料"),
    "黄金": ("贵金属", "黄金"),
    "煤炭": ("煤炭", "动力煤"),
    "钢铁": ("钢铁", "钢材"),
    "有色金属": ("有色金属", "铜铝等"),
    "化工": ("化工", "化工新材料"),
    "PEEK": ("高性能材料", "PEEK材料"),
    "碳纤维": ("碳纤维", "碳纤维复合材料"),
    "工业母机": ("工业母机", "高端数控机床"),
    "3D打印": ("3D打印", "工业级3D打印机"),
}


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

    def recommend_sectors(self, top_n: int = 5) -> list[SectorRecommendation]:
        """从热门板块中推荐 top_n 个赛道，映射为产业方向+终端产品。

        Args:
            top_n: 返回推荐赛道数量，默认 5。

        Returns:
            SectorRecommendation 列表，按综合热度降序排列。
        """
        try:
            result = self.detect()
        except Exception as e:
            logger.warning(f"热门板块检测失败，返回空推荐列表: {e}")
            return []

        if not result.all_ranked:
            return []

        recommendations: list[SectorRecommendation] = []
        seen_sectors: set[str] = set()  # 去重：同一产业方向只取第一个

        for hs in result.all_ranked:
            if len(recommendations) >= top_n:
                break

            # 模糊匹配：遍历映射字典，检查板块名是否包含关键词
            sector_name = None
            end_product = None
            for keyword, (sec, prod) in BOARD_TO_SECTOR.items():
                if keyword in hs.name:
                    sector_name = sec
                    end_product = prod
                    break

            # 未匹配到的板块，直接用板块名构造
            if sector_name is None:
                sector_name = hs.name
                end_product = hs.name + "相关产品"

            # 去重：同一产业方向只保留热度最高的
            if sector_name in seen_sectors:
                continue
            seen_sectors.add(sector_name)

            # 构造推荐理由，包含具体数据
            reason_parts: list[str] = []
            if hs.price_change_pct is not None:
                sign = "+" if hs.price_change_pct >= 0 else ""
                reason_parts.append(f"涨幅{sign}{hs.price_change_pct:.2f}%")
            if hs.main_net_inflow is not None:
                reason_parts.append(f"主力净流入{hs.main_net_inflow:.1f}亿")
            if hs.turnover_rate is not None:
                reason_parts.append(f"换手率{hs.turnover_rate:.1f}%")
            if hs.up_count is not None and hs.down_count is not None:
                reason_parts.append(f"涨{hs.up_count}/跌{hs.down_count}")
            if hs.leader_stock:
                reason_parts.append(f"领涨股:{hs.leader_stock}")

            reason = ", ".join(reason_parts) if reason_parts else "热门板块"

            recommendations.append(
                SectorRecommendation(
                    sector=sector_name,
                    end_product=end_product,
                    reason=reason,
                    score=hs.composite_score,
                    market="a_stock",
                    source_board=hs.name,
                )
            )

        return recommendations

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


# ── LLM-Hybrid Hot Sector Recommendations ────────────────────────────

import asyncio
import json
import time

_hot_scan_cache: dict[str, tuple[float, list[dict]]] = {}
_HOT_SCAN_TTL = 1800  # 30 minutes


async def llm_recommend_hot_sectors(
    provider: str,
    model: str,
    top_n: int = 8,
) -> list[dict]:
    """LLM 智能热点赛道推荐。

    1. 尝试 AKShare 获取板块涨幅前10作为参考（5秒超时）
    2. 用 LLM 生成结构化推荐（保证可靠输出）
    3. 结果缓存30分钟
    """
    from bottleneck_hunter.llm_clients.factory import create_llm
    from langchain_core.messages import SystemMessage, HumanMessage

    cache_key = f"{provider}::{model}"
    if cache_key in _hot_scan_cache:
        ts, cached = _hot_scan_cache[cache_key]
        if time.time() - ts < _HOT_SCAN_TTL:
            return cached

    # ── Step 1: AKShare 板块数据（best-effort）──
    market_context = ""
    try:
        def _fetch_boards():
            try:
                df = ak.stock_board_concept_name_em()
                if df is not None and not df.empty:
                    top10 = df.head(10)
                    lines = []
                    for _, row in top10.iterrows():
                        name = row.get("板块名称", "")
                        chg = row.get("涨跌幅", 0)
                        lines.append(f"{name}（涨跌幅{chg:.1f}%）")
                    return "；".join(lines)
            except Exception:
                pass
            return ""

        market_context = await asyncio.wait_for(
            asyncio.to_thread(_fetch_boards), timeout=5.0
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"AKShare 板块数据获取失败，使用纯 LLM 推荐: {e}")

    # ── Step 2: LLM 推荐 ──
    system_prompt = (
        "你是中国A股市场投资研究专家，精通板块轮动分析和热点主题追踪。"
        "你需要推荐当前最值得关注的热门投资赛道。"
        "每个赛道必须提供：产业方向名称（sector）、具体终端产品名称（end_product）、简短推荐理由（reason，10-20字）。"
        "只推荐真实存在且当前有明确催化剂或市场热度的方向。"
        "返回纯JSON数组，不加任何其他文字或markdown标记。"
    )

    if market_context:
        user_prompt = (
            f"当前东方财富A股概念板块涨幅前10（供参考）：\n{market_context}\n\n"
            f"请基于以上实时数据和你对市场趋势的了解，推荐 {top_n} 个当前A股最热门的投资赛道。\n"
            f'返回JSON数组：[{{"sector": "产业方向", "end_product": "终端产品", "reason": "推荐理由", "market": "a_stock"}}]'
        )
    else:
        user_prompt = (
            f"请基于你对近期A股市场趋势和板块轮动的了解，推荐 {top_n} 个当前A股最热门的投资赛道。\n"
            f'返回JSON数组：[{{"sector": "产业方向", "end_product": "终端产品", "reason": "推荐理由", "market": "a_stock"}}]'
        )

    try:
        llm = create_llm(provider, model, temperature=0.3)
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=30.0)
        text = resp.content.strip()

        # 解析 JSON — 剥离可能的 markdown code fence
        if "```" in text:
            import re
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()

        results = json.loads(text)
        if not isinstance(results, list):
            results = []

        # 标准化字段
        clean = []
        for item in results[:top_n]:
            if isinstance(item, dict) and item.get("sector") and item.get("end_product"):
                clean.append({
                    "sector": str(item["sector"]),
                    "end_product": str(item["end_product"]),
                    "reason": str(item.get("reason", "")),
                    "market": str(item.get("market", "a_stock")),
                })

        _hot_scan_cache[cache_key] = (time.time(), clean)
        logger.info(f"LLM 热点推荐完成: {len(clean)} 个赛道")
        return clean

    except asyncio.TimeoutError:
        logger.error("LLM 热点推荐超时")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"LLM 返回非 JSON 格式: {e}")
        return []
    except Exception as e:
        logger.error(f"LLM 热点推荐失败: {e}")
        return []
