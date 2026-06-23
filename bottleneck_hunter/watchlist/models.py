"""Pydantic models for the watchlist tracking system."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WatchlistTier(str, Enum):
    FOCUS = "focus"    # 重点关注 (max 6)
    NORMAL = "normal"  # 一般关注 (max 6)
    TRACK = "track"    # 潜力跟踪 (max 12)


TIER_LIMITS = {
    WatchlistTier.FOCUS: 6,
    WatchlistTier.NORMAL: 6,
    WatchlistTier.TRACK: 12,
}

WATCHLIST_MAX = 24


class DegradationMode(str, Enum):
    FULL = "full"        # 所有 LLM 功能正常
    REDUCED = "reduced"  # 跳过情绪分析等非核心 LLM 调用
    MINIMAL = "minimal"  # 仅更新数据，不调用 LLM


# ---------------------------------------------------------------------------
# Watchlist entry
# ---------------------------------------------------------------------------

class WatchlistEntry(BaseModel):
    id: str = ""
    ticker: str
    company_name: str
    company_name_cn: str = ""
    market: str = "us_stock"
    tier: WatchlistTier = WatchlistTier.TRACK
    tier_rank: int = 0
    composite_score: float = 0.0
    source: str = "manual"  # "phase4" | "manual"
    source_analysis_id: Optional[str] = None
    sector: str = ""
    bottleneck_node: str = ""
    added_at: str = ""
    updated_at: str = ""
    notes: str = ""
    is_active: bool = True


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class MarketSnapshot(BaseModel):
    ticker: str
    date: str  # YYYY-MM-DD
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[int] = None
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    change_pct: Optional[float] = None
    rsi_14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    fetched_at: str = ""


class NewsDigest(BaseModel):
    id: str = ""
    ticker: str
    date: str
    title: str
    summary: str = ""
    sentiment: str = ""  # positive / negative / neutral
    sentiment_score: float = 0.0  # -1.0 ~ 1.0
    source_url: str = ""
    source_name: str = ""
    llm_analysis: str = ""
    fetched_at: str = ""


class SecFiling(BaseModel):
    id: str = ""
    ticker: str
    filing_type: str  # "4" | "8-K" | "10-Q" | "10-K"
    filed_date: str
    title: str = ""
    summary: str = ""
    url: str = ""
    is_insider_trade: bool = False
    fetched_at: str = ""


class InsiderTrade(BaseModel):
    id: str = ""
    ticker: str
    insider_name: str
    insider_title: str = ""
    transaction_type: str = ""  # "buy" | "sell" | "exercise"
    shares: int = 0
    price: Optional[float] = None
    total_value: Optional[float] = None
    date: str = ""
    source_filing_id: str = ""
    fetched_at: str = ""


class OptionsActivity(BaseModel):
    id: str = ""
    ticker: str
    date: str = ""
    unusual_volume: bool = False
    put_call_ratio: Optional[float] = None
    total_call_volume: int = 0
    total_put_volume: int = 0
    max_oi_strike: Optional[float] = None
    max_oi_expiry: str = ""
    notable_trades: list[dict] = Field(default_factory=list)
    fetched_at: str = ""


class EarningsReport(BaseModel):
    id: str = ""
    ticker: str
    report_date: str = ""
    fiscal_quarter: str = ""
    eps_actual: Optional[float] = None
    eps_estimate: Optional[float] = None
    eps_surprise_pct: Optional[float] = None
    revenue_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    guidance: str = ""
    fetched_at: str = ""


# ---------------------------------------------------------------------------
# LLM Budget
# ---------------------------------------------------------------------------

class LlmUsageRecord(BaseModel):
    date: str  # YYYY-MM-DD
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    task_type: str = ""  # news_summary | sentiment | scoring | briefing


# ---------------------------------------------------------------------------
# Pipeline status
# ---------------------------------------------------------------------------

class PipelineStatus(BaseModel):
    pipeline_name: str
    last_run_at: Optional[str] = None
    last_status: str = "idle"  # idle | running | success | error
    last_error: str = ""
    next_run_at: Optional[str] = None
    stocks_processed: int = 0
    stocks_total: int = 0


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------

class AddToWatchlistRequest(BaseModel):
    ticker: str
    company_name: str
    company_name_cn: str = ""
    market: str = "us_stock"
    tier: WatchlistTier = WatchlistTier.TRACK
    source: str = "manual"
    source_analysis_id: Optional[str] = None
    sector: str = ""
    bottleneck_node: str = ""
    notes: str = ""


class UpdateWatchlistRequest(BaseModel):
    tier: Optional[WatchlistTier] = None
    tier_rank: Optional[int] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class UpdateBudgetRequest(BaseModel):
    daily_limit_usd: Optional[float] = None
    monthly_limit_usd: Optional[float] = None
