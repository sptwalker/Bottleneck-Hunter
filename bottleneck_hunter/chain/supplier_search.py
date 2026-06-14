"""Supplier search across A-stock (AKShare) and US stock (yfinance) markets.

For each bottleneck node, searches for candidate supplier companies
by matching industry/concept boards and filtering by market cap,
institutional ownership, and fundamentals.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Sequence

import akshare as ak
import pandas as pd
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.models import (
    BottleneckReport,
    MarketRegion,
    SupplierInfo,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# A-Stock helpers
# ---------------------------------------------------------------------------

def _normalize_a_code(ticker: str) -> str:
    """Strip exchange suffix to get 6-digit code."""
    return ticker.replace(".SS", "").replace(".SZ", "").replace(".BJ", "")


def _a_stock_code_to_ticker(code: str) -> str:
    """Convert 6-digit code to yfinance-style ticker."""
    if code.startswith(("6",)):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def search_a_stock_concepts(keyword: str) -> list[str]:
    """Find concept board names matching a keyword."""
    try:
        df = ak.stock_board_concept_name_em()
        matches = df[df["板块名称"].str.contains(keyword, na=False)]
        return matches["板块名称"].tolist()
    except Exception:
        logger.warning(f"Failed to search concept boards for: {keyword}")
        return []


def search_a_stock_industries(keyword: str) -> list[str]:
    """Find industry board names matching a keyword."""
    try:
        df = ak.stock_board_industry_name_em()
        matches = df[df["板块名称"].str.contains(keyword, na=False)]
        return matches["板块名称"].tolist()
    except Exception:
        logger.warning(f"Failed to search industry boards for: {keyword}")
        return []


def get_concept_constituents(concept_name: str) -> pd.DataFrame:
    """Get all companies in a concept board."""
    try:
        df = ak.stock_board_concept_cons_em(symbol=concept_name)
        return df
    except Exception:
        logger.warning(f"Failed to get constituents for concept: {concept_name}")
        return pd.DataFrame()


def get_industry_constituents(industry_name: str) -> pd.DataFrame:
    """Get all companies in an industry board."""
    try:
        df = ak.stock_board_industry_cons_em(symbol=industry_name)
        return df
    except Exception:
        logger.warning(f"Failed to get constituents for industry: {industry_name}")
        return pd.DataFrame()


def get_a_stock_info(code: str) -> dict | None:
    """Get individual stock fundamentals from East Money."""
    try:
        df = ak.stock_individual_info_em(symbol=code)
        if df is None or df.empty:
            return None
        result = {}
        for _, row in df.iterrows():
            result[str(row["item"])] = row["value"]
        return result
    except Exception:
        logger.debug(f"Failed to get info for A-stock: {code}")
        return None


def _parse_market_cap(value) -> float | None:
    """Parse market cap string to float (in 亿)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    # Remove commas
    s = s.replace(",", "")
    try:
        v = float(s)
        # If the value is very large (>1e8), it's probably in yuan, convert to 亿
        if v > 1e8:
            return round(v / 1e8, 2)
        return v
    except ValueError:
        return None


def _parse_pct(value) -> float | None:
    """Parse percentage string to float."""
    if value is None:
        return None
    s = str(value).strip().replace("%", "").replace("％", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _a_stock_df_to_suppliers(df: pd.DataFrame, max_market_cap_yi: float | None = None) -> list[SupplierInfo]:
    """Convert a constituents DataFrame to SupplierInfo list."""
    results = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).strip()
        if not code or not code.isdigit():
            continue

        name = str(row.get("名称", ""))
        market_cap = _parse_market_cap(row.get("总市值", row.get("市值", None)))

        # Filter by market cap
        if max_market_cap_yi and market_cap and market_cap > max_market_cap_yi:
            continue

        ticker = _a_stock_code_to_ticker(code)
        results.append(
            SupplierInfo(
                name=name,
                ticker=ticker,
                market=MarketRegion.A_STOCK,
                market_cap=market_cap,
                sector=str(row.get("行业", "")),
                description=f"{name} ({code})",
                key_products=[],
                pe_ratio=_parse_pct(row.get("市盈率", row.get("滚动市盈率", None))),
            )
        )
    return results


# ---------------------------------------------------------------------------
# US Stock helpers
# ---------------------------------------------------------------------------

def search_us_by_industry(keyword: str) -> list[SupplierInfo]:
    """Search US stocks by industry keyword using yfinance.

    Note: yfinance doesn't have a bulk search API. We use a curated
    mapping of industries to common ticker lists, then filter.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed, skipping US stock search")
        return []

    # We'll use a LLM-assisted approach in supplier_eval instead.
    # Here we just return an empty list; the eval step will use the
    # bottleneck node info to identify specific tickers.
    return []


def get_us_stock_info(ticker: str) -> dict | None:
    """Get US stock fundamentals from yfinance."""
    try:
        import yfinance as yf

        stock = yf.Ticker(ticker)
        info = stock.info
        if not info:
            return None
        return {
            "name": info.get("shortName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "revenue_growth": info.get("revenueGrowth"),
            "gross_margin": info.get("grossMargins"),
            "profit_margin": info.get("profitMargins"),
            "roe": info.get("returnOnEquity"),
        }
    except Exception:
        logger.debug(f"Failed to get info for US stock: {ticker}")
        return None


# ---------------------------------------------------------------------------
# Unified search
# ---------------------------------------------------------------------------

class SupplierSearcher:
    """Search for candidate suppliers across markets."""

    def __init__(
        self,
        market: MarketRegion = MarketRegion.A_STOCK,
        max_market_cap_yi: float | None = 200,
        max_results: int = 20,
        language: str = "zh",
        llm: BaseChatModel | None = None,
    ):
        self.market = market
        self.max_market_cap_yi = max_market_cap_yi
        self.max_results = max_results
        self.language = language
        self.llm = llm

    async def search(
        self, bottleneck: BottleneckReport, keywords: list[str] | None = None
    ) -> list[SupplierInfo]:
        """Search for suppliers related to a bottleneck node."""
        # 使用 LLM 扩展关键词，提高东方财富板块匹配率
        if keywords:
            terms = list(keywords)
        elif self.llm:
            terms = await self._expand_keywords(bottleneck)
        else:
            terms = [bottleneck.node_name]

        # 始终包含原始节点名作为兜底搜索词
        if bottleneck.node_name not in terms:
            terms.append(bottleneck.node_name)

        logger.info(f"搜索供应商 [{bottleneck.node_name}]，关键词: {terms}")

        suppliers: list[SupplierInfo] = []

        if self.market in (MarketRegion.A_STOCK, MarketRegion.ALL):
            a_suppliers = await self._search_a_stock(terms)
            suppliers.extend(a_suppliers)

        if self.market in (MarketRegion.US_STOCK, MarketRegion.ALL):
            us_suppliers = await self._search_us_stock(terms, bottleneck)
            suppliers.extend(us_suppliers)

        # Deduplicate by ticker
        seen = set()
        unique = []
        for s in suppliers:
            if s.ticker not in seen:
                seen.add(s.ticker)
                unique.append(s)

        if not unique:
            logger.warning(f"未找到任何供应商: {bottleneck.node_name}（关键词: {terms}）")

        return unique[: self.max_results]

    async def _expand_keywords(self, bottleneck: BottleneckReport) -> list[str]:
        """使用 LLM 将瓶颈环节名称扩展为多个东方财富板块搜索关键词。"""
        prompt = f"""你是 A 股市场研究助手。给定一个产业链瓶颈环节，请生成 3-5 个关键词用于在东方财富网的概念板块和行业板块中搜索相关上市公司。

关键词要求：
1. 每个关键词是 2-4 个汉字，尽量短
2. 应该是东方财富板块名称中可能包含的子串
3. 覆盖不同角度：材料名、技术名、应用领域、行业分类
4. 不要太具体也不要太宽泛

瓶颈环节名称: {bottleneck.node_name}
环节描述: {bottleneck.node_description}
关键洞察: {', '.join(bottleneck.key_insights[:3]) if bottleneck.key_insights else '无'}

请直接返回一个 JSON 数组，例如: ["光模块", "光通信", "半导体"]
不要返回其他内容。"""

        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            text = response.content.strip()
            # 剥离 markdown code fence
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:])
                if text.endswith("```"):
                    text = text[:-3].strip()
            import json
            keywords = json.loads(text)
            if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
                logger.info(f"LLM 关键词扩展 [{bottleneck.node_name}] → {keywords}")
                return keywords
        except Exception:
            logger.warning(f"LLM 关键词扩展失败 [{bottleneck.node_name}]，回退到原始名称")
        return [bottleneck.node_name]

    async def _search_a_stock(self, terms: list[str]) -> list[SupplierInfo]:
        """Search A-stock market for suppliers."""
        all_suppliers: list[SupplierInfo] = []

        for term in terms:
            matched_any = False
            # Search concept boards
            concepts = search_a_stock_concepts(term)
            if concepts:
                matched_any = True
                logger.debug(f"关键词 [{term}] 匹配到概念板块: {concepts}")
            for concept in concepts:
                df = get_concept_constituents(concept)
                if not df.empty:
                    suppliers = _a_stock_df_to_suppliers(df, self.max_market_cap_yi)
                    all_suppliers.extend(suppliers)

            # Search industry boards
            industries = search_a_stock_industries(term)
            if industries:
                matched_any = True
                logger.debug(f"关键词 [{term}] 匹配到行业板块: {industries}")
            for industry in industries:
                df = get_industry_constituents(industry)
                if not df.empty:
                    suppliers = _a_stock_df_to_suppliers(df, self.max_market_cap_yi)
                    all_suppliers.extend(suppliers)

            if not matched_any:
                logger.warning(f"关键词 [{term}] 未匹配到任何概念或行业板块")

        # Enrich with fundamentals
        enriched = await self._enrich_a_stocks(all_suppliers)
        return enriched

    async def _search_us_stock(
        self, terms: list[str], bottleneck: BottleneckReport
    ) -> list[SupplierInfo]:
        """Search US stock market for suppliers.

        Uses a simple approach: returns empty here since bulk search
        isn't available via yfinance. The LLM eval step will
        identify specific tickers.
        """
        return []

    async def _enrich_a_stocks(self, suppliers: list[SupplierInfo]) -> list[SupplierInfo]:
        """Enrich supplier info with detailed fundamentals."""
        enriched = []
        for s in suppliers[:self.max_results * 2]:  # Limit API calls
            code = _normalize_a_code(s.ticker)
            info = get_a_stock_info(code)
            if info:
                s.description = info.get("公司介绍", s.description) or s.description
                s.sector = info.get("行业", s.sector) or s.sector
                market_cap_str = info.get("总市值", None)
                if market_cap_str and s.market_cap is None:
                    s.market_cap = _parse_market_cap(market_cap_str)
                pe_str = info.get("市盈率(动态)", None)
                if pe_str and s.pe_ratio is None:
                    s.pe_ratio = _parse_pct(pe_str)
            enriched.append(s)
        return enriched

    async def search_bottlenecks(
        self, bottlenecks: list[BottleneckReport]
    ) -> dict[str, list[SupplierInfo]]:
        """Search suppliers for multiple bottleneck nodes.

        Returns a mapping of node_name -> supplier list.
        """
        result = {}
        for bn in bottlenecks:
            suppliers = await self.search(bn)
            result[bn.node_name] = suppliers
            logger.info(f"Found {len(suppliers)} suppliers for {bn.node_name}")
        return result
