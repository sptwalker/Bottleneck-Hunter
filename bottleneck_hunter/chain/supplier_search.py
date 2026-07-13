"""Supplier search: LLM recommends candidates, market API validates.

Architecture:
  1. LLM recommends 10-15 candidate companies per bottleneck node
  2. Market-specific API validates tickers & fetches real-time data:
     - A-stock: Tencent qt.gtimg.cn
     - US-stock: yfinance
  3. Filter by market cap, deduplicate, return enriched SupplierInfo list
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import Callable, Coroutine
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from bottleneck_hunter.chain.json_utils import extract_json_array as _extract_json_array
from bottleneck_hunter.chain.models import (
    BottleneckReport,
    ChainGraph,
    MarketRegion,
    SupplierInfo,
)

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------------------
# A-stock helpers
# ---------------------------------------------------------------------------

def _code_to_tencent(code: str) -> str:
    """6-digit A-stock code → Tencent symbol (sh/sz prefix)."""
    code = code.strip()
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def _code_to_ticker(code: str) -> str:
    """6-digit code → yfinance-style ticker with exchange suffix."""
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """Batch-fetch real-time quotes from Tencent finance API (A-stock only)."""
    if not codes:
        return {}

    symbols = [_code_to_tencent(c) for c in codes]
    url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)

    try:
        resp = urllib.request.urlopen(req, timeout=10)
        text = resp.read().decode("gbk", errors="replace")
    except Exception:
        logger.warning("腾讯行情 API 请求失败")
        return {}

    results: dict[str, dict] = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"v_(\w+)=\"(.*)\"", line)
        if not m:
            continue
        fields = m.group(2).split("~")
        if len(fields) < 46 or not fields[1]:
            continue
        code = fields[2]
        try:
            total_mcap = float(fields[45]) if fields[45] else 0
            pe = float(fields[39]) if fields[39] else None
            price = float(fields[3]) if fields[3] else 0
        except (ValueError, IndexError):
            continue

        results[code] = {
            "name": fields[1],
            "code": code,
            "price": price,
            "total_mcap_yi": round(total_mcap, 2),
            "pe": pe,
        }

    return results


# ---------------------------------------------------------------------------
# US-stock helpers
# ---------------------------------------------------------------------------

def fetch_yfinance_quotes(tickers: list[str]) -> dict[str, dict]:
    """Batch-fetch quotes for US-stock tickers via yfinance.

    Filters out OTC / Pink Sheets tickers — only NYSE, NASDAQ, AMEX are accepted.

    Returns:
        dict of ticker -> {name, price, market_cap_b, pe}
    """
    if not tickers:
        return {}

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance 未安装，无法验证美股行情")
        return {}

    VALID_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX"}

    results: dict[str, dict] = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            exchange = info.get("exchange", "")
            if exchange and exchange not in VALID_EXCHANGES:
                logger.info(f"跳过非主板 ticker {ticker} (exchange={exchange})")
                continue
            name = info.get("shortName") or info.get("longName") or ticker
            mcap = info.get("marketCap")
            market_cap_b = round(mcap / 1e9, 2) if mcap else None
            pe = info.get("trailingPE")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if name and name != ticker:
                results[ticker] = {
                    "name": name,
                    "ticker": ticker,
                    "price": price,
                    "market_cap_b": market_cap_b,
                    "pe": round(pe, 2) if pe else None,
                }
        except Exception:
            logger.debug(f"yfinance 查询失败: {ticker}")
            continue

    return results


# ---------------------------------------------------------------------------
# AKShare fallback (A-stock only)
# ---------------------------------------------------------------------------

def _try_akshare_search(terms: list[str], max_market_cap_yi: float | None) -> list[SupplierInfo]:
    """Try AKShare concept/industry board search. Returns empty on failure."""
    try:
        import akshare as ak
    except ImportError:
        return []

    suppliers: list[SupplierInfo] = []

    for term in terms:
        for search_fn, cons_fn in [
            (ak.stock_board_concept_name_em, ak.stock_board_concept_cons_em),
            (ak.stock_board_industry_name_em, ak.stock_board_industry_cons_em),
        ]:
            try:
                df_boards = search_fn()
                matches = df_boards[df_boards["板块名称"].str.contains(term, na=False)]
                for board_name in matches["板块名称"].tolist()[:3]:
                    try:
                        df_cons = cons_fn(symbol=board_name)
                    except Exception:
                        continue
                    if df_cons is None or df_cons.empty:
                        continue
                    board_count = 0
                    for _, row in df_cons.iterrows():
                        if board_count >= 10:
                            break
                        code = str(row.get("代码", "")).strip()
                        if not code or not code.isdigit():
                            continue
                        name = str(row.get("名称", ""))
                        mcap_raw = row.get("总市值", row.get("市值", None))
                        mcap = None
                        if mcap_raw is not None:
                            try:
                                v = float(str(mcap_raw).replace(",", ""))
                                mcap = round(v / 1e8, 2) if v > 1e8 else v
                            except ValueError:
                                pass
                        if max_market_cap_yi and mcap and mcap > max_market_cap_yi:
                            continue
                        suppliers.append(SupplierInfo(
                            name=name,
                            name_cn="",
                            ticker=_code_to_ticker(code),
                            market=MarketRegion.A_STOCK,
                            market_cap=mcap,
                            sector=str(row.get("行业", "")),
                            description=f"{name} ({code})",
                            key_products=[],
                            source="akshare",
                        ))
                        board_count += 1
            except Exception:
                continue

    logger.info(f"AKShare 板块搜索: 关键词 {terms} → {len(suppliers)} 家")
    return suppliers


# ---------------------------------------------------------------------------
# Market-specific prompt / validation config
# ---------------------------------------------------------------------------

_ASTOCK_PROMPT = """你是一位资深 A 股行业研究员。请根据以下产业链瓶颈环节，推荐 10-15 家最相关的候选供应商上市公司。

## 瓶颈环节
- 名称: {node_name}
- 描述: {node_desc}
- 关键洞察: {insights}

## 筛选条件
- 仅限 A 股上市公司（沪深北交所均可）
- {cap_hint}
- 优先选择在该环节有核心技术或显著市占率的公司
- 同时包含行业龙头和被低估的隐形冠军
- 确保推荐的公司确实与该瓶颈环节有实质业务关联

## 重要注意事项
- 只推荐当前仍在交易的上市公司，不要推荐已退市、已被收购、或未上市的公司
- 股票代码必须是真实可查的，能在沪深北交易所找到
- 公司简称必须与交易所登记的名称一致，不要使用集团全称或英文名
- 代码格式：沪市6开头、深市0或3开头、北交所4或8开头，共6位数字

## 返回格式
请返回严格 JSON 数组，每个元素包含:
- name: 公司简称（如"北方华创"，必须是在交易所正式使用的名称）
- name_cn: 公司中文全称（如"北方华创科技集团股份有限公司"）
- code: 6位股票代码（如"002371"，不要加交易所后缀）
- sector: 所属细分行业
- description: 用中文一句话介绍该公司在此环节的地位和核心竞争力
- key_products: 与该瓶颈相关的主要产品列表（2-3个，中文）

只返回 JSON 数组，不要其他文字。"""

_US_STOCK_PROMPT = """You are a senior equity research analyst specializing in US stock markets. Based on the following supply chain bottleneck, recommend 10-15 most relevant publicly-traded supplier companies.

## Bottleneck Node
- Name: {node_name}
- Description: {node_desc}
- Key Insights: {insights}

## Selection Criteria
- US-listed companies ONLY (NYSE, NASDAQ, AMEX)
- {cap_hint}
- Prioritize companies with core technology or significant market share in this segment
- Include both industry leaders and undervalued hidden champions
- Ensure companies have substantial business relevance to this bottleneck

## Important Rules
- Only recommend companies that are CURRENTLY listed and actively trading on NYSE, NASDAQ, or AMEX
- Do NOT recommend private companies, delisted companies, or companies acquired by others
- Ticker must be a real, verifiable US stock symbol (e.g. NVDA, ASML, AMAT)
- Do NOT recommend companies listed only on foreign exchanges (e.g. Tokyo, Frankfurt, London)
- Company name must match the official SEC filing name

## Return Format
Return a strict JSON array, each element containing:
- name: Company name (e.g. "NVIDIA", must be the official trading name)
- name_cn: Chinese name of the company (e.g. "英伟达")
- ticker: Stock ticker symbol (e.g. "NVDA", uppercase, no exchange suffix)
- sector: Sub-industry classification
- description: One sentence IN CHINESE about the company's position and competitive advantage in this segment
- key_products: List of 2-3 key products related to this bottleneck (in Chinese)

Return ONLY the JSON array, no other text."""

_ALL_MARKET_PROMPT = """你是一位资深全球行业研究员。请根据以下产业链瓶颈环节，推荐 10-15 家最相关的候选供应商上市公司（A 股或美股均可）。

## 瓶颈环节
- 名称: {node_name}
- 描述: {node_desc}
- 关键洞察: {insights}

## 筛选条件
- A 股（沪深北交所）或美股（NYSE/NASDAQ）上市公司均可
- {cap_hint}
- 优先选择在该环节有核心技术或显著市占率的公司
- 同时包含行业龙头和被低估的隐形冠军
- 确保推荐的公司确实与该瓶颈环节有实质业务关联

## 重要注意事项
- 只推荐当前仍在交易的上市公司，不要推荐已退市、被收购或未上市的公司
- A 股代码必须是真实的6位数字（沪市6开头、深市0或3开头、北交所4或8开头）
- 美股 ticker 必须是 NYSE/NASDAQ/AMEX 上真实可查的代码
- 不要推荐仅在其他国家交易所上市的公司（如东京、法兰克福、伦敦等）

## 返回格式
请返回严格 JSON 数组，每个元素包含:
- name: 公司名称（如"北方华创"或"NVIDIA"）
- name_cn: 公司中文名称（如"北方华创科技集团股份有限公司"或"英伟达"）
- ticker: 股票代码（A 股用6位数字如"002371"，美股用字母如"NVDA"）
- market: "a_stock" 或 "us_stock"
- sector: 所属细分行业
- description: 用中文一句话介绍该公司在此环节的地位和核心竞争力
- key_products: 与该瓶颈相关的主要产品列表（2-3个，中文）

只返回 JSON 数组，不要其他文字。"""


# ---------------------------------------------------------------------------
# Main searcher
# ---------------------------------------------------------------------------

class SupplierSearcher:
    """Market-aware supplier search with LLM recommendation and API validation."""

    LLM_TIMEOUT = 120
    MAX_CONCURRENCY = 4
    MAX_RETRIES = 2

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
        self._on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None
        self._timeout_count = 0
        self._retry_count = 0

    @property
    def _is_us(self) -> bool:
        return self.market == MarketRegion.US_STOCK

    @property
    def _is_all(self) -> bool:
        return self.market == MarketRegion.ALL

    async def _emit(self, msg: str):
        if self._on_progress:
            await self._on_progress(msg)
        logger.info(msg)

    async def search(
        self, bottleneck: BottleneckReport, keywords: list[str] | None = None,
        chain_graph: ChainGraph | None = None,
    ) -> list[SupplierInfo]:
        """Search for suppliers related to a bottleneck node (multi-source)."""

        # --- 并行执行三路搜索 ---
        async def _llm_source():
            if not self.llm:
                return []
            return await self._llm_recommend(bottleneck)

        async def _akshare_source():
            if self._is_us:
                return []
            terms = list(keywords) if keywords else self._extract_keywords(bottleneck.node_name)
            if bottleneck.key_insights:
                for insight in bottleneck.key_insights[:2]:
                    extra = self._extract_keywords(insight)
                    terms.extend(extra)
                terms = list(dict.fromkeys(terms))[:6]
            try:
                results = await asyncio.to_thread(
                    _try_akshare_search, terms, self.max_market_cap_yi
                )
                for s in results:
                    s.source = "akshare"
                return results
            except Exception:
                logger.exception("AKShare 搜索异常")
                return []

        async def _chain_source():
            if not chain_graph:
                return []
            return self._extract_chain_candidates(bottleneck, chain_graph)

        llm_results, akshare_results, chain_results = await asyncio.gather(
            _llm_source(), _akshare_source(), _chain_source(),
        )

        # --- 按 ticker 去重合并（LLM 优先 > chain > akshare）---
        merged: dict[str, SupplierInfo] = {}
        source_stats = {"llm": 0, "chain": 0, "akshare": 0}

        for supplier in llm_results:
            if supplier.ticker not in merged:
                merged[supplier.ticker] = supplier
                source_stats["llm"] += 1

        for supplier in chain_results:
            if supplier.ticker not in merged:
                merged[supplier.ticker] = supplier
                source_stats["chain"] += 1

        for supplier in akshare_results:
            if supplier.ticker not in merged:
                merged[supplier.ticker] = supplier
                source_stats["akshare"] += 1

        unique = list(merged.values())

        # --- 进度消息 ---
        parts = []
        if source_stats["llm"]:
            parts.append(f"LLM {source_stats['llm']} 家")
        if source_stats["chain"]:
            parts.append(f"产业链 {source_stats['chain']} 家")
        if source_stats["akshare"]:
            parts.append(f"板块 {source_stats['akshare']} 家")

        if not unique:
            await self._emit(f"✗ 未找到供应商: {bottleneck.node_name}")
        else:
            await self._emit(
                f"✓ {bottleneck.node_name}: {' + '.join(parts)} → 去重后 {len(unique)} 家"
            )

        return unique[: self.max_results]

    # ----- LLM recommendation (market-aware) ---------------------------------

    def _build_prompt(self, bottleneck: BottleneckReport) -> tuple[str, str]:
        """Build market-specific system message and user prompt."""
        insights = ", ".join(bottleneck.key_insights[:3]) if bottleneck.key_insights else "无"

        if self._is_us:
            cap_hint = (
                f"Prefer companies with market cap under ${self.max_market_cap_yi / 10:.0f}B "
                f"(i.e. {self.max_market_cap_yi}亿美元)"
                if self.max_market_cap_yi else ""
            )
            sys_msg = (
                "You are a supply chain research expert focused on US public equities. "
                "Ticker symbols must be real, currently-trading US stock tickers."
            )
            user_msg = _US_STOCK_PROMPT.format(
                node_name=bottleneck.node_name,
                node_desc=bottleneck.node_description,
                insights=insights,
                cap_hint=cap_hint,
            )
        elif self._is_all:
            cap_hint = (
                f"优先推荐市值在 {self.max_market_cap_yi} 亿（A股按人民币、美股约 ${self.max_market_cap_yi / 10:.0f}B）以下的中小盘股"
                if self.max_market_cap_yi else ""
            )
            sys_msg = "你是产业链供应商研究专家，精通 A 股和美股市场。股票代码必须是真实、当前仍在交易的代码。"
            user_msg = _ALL_MARKET_PROMPT.format(
                node_name=bottleneck.node_name,
                node_desc=bottleneck.node_description,
                insights=insights,
                cap_hint=cap_hint,
            )
        else:
            cap_hint = (
                f"优先推荐市值在 {self.max_market_cap_yi} 亿元以下的中小盘股"
                if self.max_market_cap_yi else ""
            )
            sys_msg = "你是产业链供应商研究专家。股票代码必须是真实、当前仍在交易的 A 股代码。"
            user_msg = _ASTOCK_PROMPT.format(
                node_name=bottleneck.node_name,
                node_desc=bottleneck.node_description,
                insights=insights,
                cap_hint=cap_hint,
            )

        return sys_msg, user_msg

    async def _llm_recommend(self, bottleneck: BottleneckReport) -> list[SupplierInfo]:
        """LLM 推荐候选供应商 → 行情验证 → 市值过滤。带重试。"""
        sys_msg, user_msg = self._build_prompt(bottleneck)

        messages = [
            SystemMessage(content=sys_msg),
            HumanMessage(content=user_msg),
        ]

        items = None
        last_error = None
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await asyncio.wait_for(
                    self.llm.ainvoke(messages),
                    timeout=self.LLM_TIMEOUT,
                )
                text = response.content.strip()
                items = _extract_json_array(text)
                if items:
                    break
                last_error = "LLM 返回的 JSON 无法解析"
                if attempt < self.MAX_RETRIES:
                    self._retry_count += 1
                    await self._emit(
                        f"⚠ [{bottleneck.node_name}] JSON 解析失败，重试 ({attempt + 1}/{self.MAX_RETRIES})..."
                    )
            except asyncio.TimeoutError:
                self._timeout_count += 1
                last_error = f"LLM 调用超时 ({self.LLM_TIMEOUT}s)"
                if attempt < self.MAX_RETRIES:
                    self._retry_count += 1
                    await self._emit(
                        f"⚠ [{bottleneck.node_name}] LLM 超时，重试 ({attempt + 1}/{self.MAX_RETRIES})..."
                    )
            except Exception as e:
                last_error = str(e)
                if attempt < self.MAX_RETRIES:
                    self._retry_count += 1
                    await self._emit(
                        f"⚠ [{bottleneck.node_name}] LLM 调用失败: {last_error}，重试..."
                    )

        if not items:
            await self._emit(f"✗ [{bottleneck.node_name}] LLM 推荐失败: {last_error}")
            return []

        if self._is_us:
            return await self._validate_us_candidates(items, bottleneck)
        elif self._is_all:
            return await self._validate_mixed_candidates(items, bottleneck)
        else:
            return await self._validate_astock_candidates(items, bottleneck)

    # ----- A-stock validation ------------------------------------------------

    async def _validate_astock_candidates(
        self, items: list, bottleneck: BottleneckReport
    ) -> list[SupplierInfo]:
        raw_candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code", item.get("ticker", ""))).strip()
            code = re.sub(r"\.(SZ|SS|BJ|sz|ss|bj)$", "", code)
            code = re.sub(r"^(sz|sh|bj|SZ|SH|BJ)", "", code)
            if not code or not code.isdigit() or len(code) != 6:
                continue
            if code[0] not in ("6", "0", "3", "4", "8"):
                continue
            item["_code"] = code
            raw_candidates.append(item)

        if not raw_candidates:
            await self._emit(f"⚠ [{bottleneck.node_name}] LLM 返回了 {len(items)} 条数据，但无有效 A 股代码")
            return []

        codes = [c["_code"] for c in raw_candidates]
        names = [c.get("name", "?") for c in raw_candidates]
        await self._emit(
            f"  [{bottleneck.node_name}] LLM 推荐 {len(raw_candidates)} 家: "
            + ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
        )

        try:
            quotes = await asyncio.to_thread(fetch_tencent_quotes, codes)
        except Exception:
            logger.exception("腾讯行情 API 异常")
            quotes = {}

        if not quotes:
            await self._emit(f"⚠ [{bottleneck.node_name}] A 股行情验证失败，无法确认候选有效性")
            return []

        validated: list[SupplierInfo] = []
        skipped = 0
        for item in raw_candidates:
            code = item["_code"]
            quote = quotes.get(code)
            if not quote:
                skipped += 1
                continue

            validated.append(SupplierInfo(
                name=quote["name"],
                name_cn=item.get("name_cn", ""),
                ticker=_code_to_ticker(code),
                market=MarketRegion.A_STOCK,
                market_cap=quote["total_mcap_yi"],
                sector=item.get("sector", ""),
                description=item.get("description", ""),
                key_products=item.get("key_products", []),
                pe_ratio=quote["pe"],
            ))

        if skipped:
            await self._emit(f"  [{bottleneck.node_name}] 行情验证过滤掉 {skipped} 家无效候选")

        return self._apply_cap_filter(validated, bottleneck.node_name)

    # ----- US-stock validation -----------------------------------------------

    async def _validate_us_candidates(
        self, items: list, bottleneck: BottleneckReport
    ) -> list[SupplierInfo]:
        raw_candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker", item.get("code", ""))).strip().upper()
            ticker = re.sub(r"\s+", "", ticker)
            if not ticker or not re.match(r"^[A-Z]{1,5}$", ticker):
                continue
            item["_ticker"] = ticker
            raw_candidates.append(item)

        if not raw_candidates:
            await self._emit(f"⚠ [{bottleneck.node_name}] LLM 返回了 {len(items)} 条数据，但无有效美股 ticker")
            return []

        tickers = [c["_ticker"] for c in raw_candidates]
        names = [c.get("name", "?") for c in raw_candidates]
        await self._emit(
            f"  [{bottleneck.node_name}] LLM 推荐 {len(raw_candidates)} 家: "
            + ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
        )

        try:
            quotes = await asyncio.to_thread(fetch_yfinance_quotes, tickers)
        except Exception:
            logger.exception("yfinance API 异常")
            quotes = {}

        if not quotes:
            await self._emit(f"⚠ [{bottleneck.node_name}] 美股行情验证失败，无法确认候选有效性")
            return []

        validated: list[SupplierInfo] = []
        skipped = 0
        for item in raw_candidates:
            ticker = item["_ticker"]
            quote = quotes.get(ticker)
            if not quote:
                skipped += 1
                continue

            validated.append(SupplierInfo(
                name=quote["name"],
                name_cn=item.get("name_cn", ""),
                ticker=ticker,
                market=MarketRegion.US_STOCK,
                market_cap=quote["market_cap_b"],
                sector=item.get("sector", ""),
                description=item.get("description", ""),
                key_products=item.get("key_products", []),
                pe_ratio=quote["pe"],
            ))

        if skipped:
            await self._emit(f"  [{bottleneck.node_name}] 行情验证过滤掉 {skipped} 家无效候选")

        return self._apply_cap_filter(validated, bottleneck.node_name)

    # ----- Mixed market validation -------------------------------------------

    async def _validate_mixed_candidates(
        self, items: list, bottleneck: BottleneckReport
    ) -> list[SupplierInfo]:
        """Validate candidates from mixed A-stock and US-stock results."""
        a_items = []
        us_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            mkt = str(item.get("market", "")).strip().lower()
            ticker = str(item.get("ticker", item.get("code", ""))).strip()

            if mkt == "us_stock" or (re.match(r"^[A-Za-z]{1,5}$", ticker) and mkt != "a_stock"):
                us_items.append(item)
            else:
                a_items.append(item)

        results: list[SupplierInfo] = []
        if a_items:
            results.extend(await self._validate_astock_candidates(a_items, bottleneck))
        if us_items:
            results.extend(await self._validate_us_candidates(us_items, bottleneck))

        if not results:
            return []
        return results

    # ----- Market cap filter -------------------------------------------------

    def _apply_cap_filter(
        self, candidates: list[SupplierInfo], node_name: str
    ) -> list[SupplierInfo]:
        if not self.max_market_cap_yi or not candidates:
            return candidates

        # 阈值单位统一为「亿(原生货币)」。美股候选 market_cap 存的是 $B，需 /10 换算（$1B = 10亿美元）
        cap_threshold = self.max_market_cap_yi / 10 if self._is_us else self.max_market_cap_yi

        under_cap = [
            s for s in candidates
            if s.market_cap is not None and s.market_cap <= cap_threshold
        ]
        if under_cap:
            return under_cap

        sorted_by_cap = sorted(candidates, key=lambda s: s.market_cap if s.market_cap is not None else float("inf"))
        kept = sorted_by_cap[: self.max_results]
        cap_unit = "亿美元" if self._is_us else "亿"
        logger.info(
            f"[{node_name}] 所有候选市值均超过 {self.max_market_cap_yi}{cap_unit}，"
            f"保留市值最小的 {len(kept)} 家"
        )
        return kept

    # ----- Chain graph candidate extraction -----------------------------------

    def _extract_chain_candidates(
        self, bottleneck: BottleneckReport, chain: ChainGraph
    ) -> list[SupplierInfo]:
        """从产业链图谱的 representative_companies 中提取候选供应商。"""
        candidates: list[SupplierInfo] = []
        seen_tickers: set[str] = set()

        target_nodes = []
        node = chain.get_node(bottleneck.node_name)
        if node:
            target_nodes.append(node)
            target_nodes.extend(chain.get_upstream(bottleneck.node_name))

        for n in target_nodes:
            for company in n.representative_companies:
                name = company.get("name", "").strip()
                code = company.get("code", "").strip()
                if not name or not code:
                    continue

                ticker = None
                market = None

                clean_code = re.sub(r"\.(SH|SZ|BJ|sh|sz|bj)$", "", code)
                clean_code = re.sub(r"^(sz|sh|bj|SZ|SH|BJ)", "", clean_code)

                if clean_code.isdigit() and len(clean_code) == 6 and clean_code[0] in ("6", "0", "3", "4", "8"):
                    if self._is_us:
                        continue
                    ticker = _code_to_ticker(clean_code)
                    market = MarketRegion.A_STOCK
                elif re.match(r"^[A-Z]{1,5}$", code.upper()):
                    if not self._is_us and not self._is_all:
                        continue
                    ticker = code.upper()
                    market = MarketRegion.US_STOCK
                else:
                    continue

                if ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)

                candidates.append(SupplierInfo(
                    name=name,
                    name_cn="",
                    ticker=ticker,
                    market=market,
                    sector="",
                    description=f"来自产业链节点「{n.name}」的代表企业",
                    source="chain",
                ))

        if candidates:
            logger.info(f"[{bottleneck.node_name}] 从产业链图谱提取 {len(candidates)} 家候选")

        return candidates

    # ----- Keyword extraction ------------------------------------------------

    @staticmethod
    def _extract_keywords(node_name: str) -> list[str]:
        """从节点名称中提取搜索关键词（不依赖 LLM）。"""
        for prefix in ("高端", "先进", "精密", "超高纯", "高纯", "高性能",
                        "新型", "专用", "关键", "核心", "特种"):
            node_name = node_name.removeprefix(prefix)
        parts = re.split(r"[/、及和与]", node_name)
        keywords = [p.strip() for p in parts if len(p.strip()) >= 2]
        if not keywords:
            keywords = [node_name]
        return keywords

    # ----- Batch search across all bottlenecks -------------------------------

    async def search_bottlenecks(
        self,
        bottlenecks: list[BottleneckReport],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        chain_graph: ChainGraph | None = None,
    ) -> dict[str, list[SupplierInfo]]:
        """Search suppliers for multiple bottleneck nodes concurrently."""
        self._on_progress = on_progress
        total = len(bottlenecks)
        market_label = {
            MarketRegion.A_STOCK: "A 股",
            MarketRegion.US_STOCK: "美股",
            MarketRegion.ALL: "A 股 + 美股",
        }.get(self.market, str(self.market))

        source_hint = "（多源交叉: LLM + 板块 + 产业链）" if chain_graph else ""
        await self._emit(f"── 开始检索 {total} 个瓶颈环节的供应商 (市场: {market_label}) {source_hint}──")

        result: dict[str, list[SupplierInfo]] = {}
        sem = asyncio.Semaphore(self.MAX_CONCURRENCY)

        async def _task(bn: BottleneckReport, idx: int):
            async with sem:
                await self._emit(f"▸ 检索: {bn.node_name} ({idx}/{total})")
                suppliers = await self.search(bn, chain_graph=chain_graph)
                return bn.node_name, suppliers

        tasks = [_task(bn, i + 1) for i, bn in enumerate(bottlenecks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.exception(f"供应商搜索异常: {r}")
                continue
            name, suppliers = r
            result[name] = suppliers

        total_found = sum(len(v) for v in result.values())
        await self._emit(
            f"── 供应商检索完成: 共找到 {total_found} 家候选供应商 "
            f"(超时 {self._timeout_count} 次, 重试 {self._retry_count} 次) ──"
        )
        return result
