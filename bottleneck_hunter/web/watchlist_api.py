"""Watchlist API router — mounted at /api/watchlist.

Separate from the main api.py to keep concerns isolated.
IMPORTANT: Fixed-path routes (pipeline-status, budget, refresh) MUST come
before /{entry_id} to avoid being swallowed by the path parameter.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.watchlist.models import (
    AddToWatchlistRequest,
    UpdateBudgetRequest,
    UpdateWatchlistRequest,
)
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.streaming._common import _sse
from bottleneck_hunter.web.streaming._notice import with_notices

logger = logging.getLogger(__name__)

router = APIRouter(tags=["watchlist"])

_store: WatchlistStore | None = None
_auth_store = None


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def set_auth_store(auth_store) -> None:
    """注入 AuthStore，用于解析用户观察池上限与全局分档比例配置。"""
    global _auth_store
    _auth_store = auth_store


def _get_store() -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="WatchlistStore not initialized")
    return _store


def _resolve_tier_caps(user_id: str) -> dict[str, int]:
    """按「用户上限（无则全局默认） × 全局比例配置」推导该用户的分档容量。"""
    from bottleneck_hunter.watchlist.tier_limits import derive_tier_caps, DEFAULT_TOTAL, DEFAULT_FOCUS_PCT, DEFAULT_NORMAL_PCT
    if _auth_store is None:
        return derive_tier_caps()
    total = DEFAULT_TOTAL
    try:
        u = _auth_store.get_user_by_id(user_id)
        if u is not None and getattr(u, "watchlist_limit", None):
            total = int(u.watchlist_limit)
        else:
            total = int(_auth_store.get_config("default_watchlist_limit", str(DEFAULT_TOTAL)))
        focus_pct = float(_auth_store.get_config("watchlist_tier_focus_pct", str(DEFAULT_FOCUS_PCT)))
        normal_pct = float(_auth_store.get_config("watchlist_tier_normal_pct", str(DEFAULT_NORMAL_PCT)))
    except Exception:
        logger.warning("解析用户观察池上限失败，回退默认", exc_info=True)
        return derive_tier_caps()
    return derive_tier_caps(total, focus_pct, normal_pct)


def _user_store(user: dict) -> WatchlistStore:
    """返回绑定当前用户的 store 实例（含该用户生效的分档容量）。"""
    uid = user["sub"]
    return _get_store().for_user(uid, tier_caps=_resolve_tier_caps(uid))


# ─────────────────────────────────────────────────────────────
# Fixed-path routes FIRST (before /{entry_id})
# ─────────────────────────────────────────────────────────────

@router.get("/pipeline-status")
async def pipeline_status(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.scheduler import get_job_statuses
    return {
        "pipelines": store.get_pipeline_statuses(),
        "jobs": get_job_statuses(),
    }


@router.get("/budget")
async def get_budget(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    tracker = BudgetTracker(store)
    return tracker.get_status()


@router.patch("/budget")
async def update_budget(req: UpdateBudgetRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    tracker = BudgetTracker(store)
    tracker.set_limits(daily=req.daily_limit_usd, monthly=req.monthly_limit_usd)
    return tracker.get_status()


@router.post("/refresh")
async def refresh_all(request: Request, user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.scheduler import run_manual_refresh
    store = _user_store(user)

    async def event_generator():
        async for event in run_manual_refresh(user_store=store):
            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.post("/refresh/{pipeline}")
async def refresh_pipeline(pipeline: str, request: Request, user: dict = Depends(get_current_user)):
    valid = {"price", "news", "sec", "options"}
    if pipeline not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown pipeline: {pipeline}. Valid: {valid}")

    from bottleneck_hunter.watchlist.scheduler import run_manual_refresh
    store = _user_store(user)

    async def event_generator():
        async for event in run_manual_refresh(pipeline, user_store=store):
            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


# ─────────────────────────────────────────────────────────────
# Strategy Brain — Intelligence + Strategy
# ─────────────────────────────────────────────────────────────

@router.post("/refresh-intelligence")
async def refresh_intelligence(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """SSE 流：刷新所有股票的情报聚合"""
    from bottleneck_hunter.watchlist.strategy_engine import refresh_intelligence_all
    from bottleneck_hunter.watchlist.budget import BudgetTracker

    store = _user_store(user).for_market(market)
    budget = BudgetTracker(store)

    async def event_generator():
        async for evt in refresh_intelligence_all(store, budget):
            if await request.is_disconnected():
                break
            yield {
                "event": evt.get("event", "progress"),
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.post("/refresh-strategy")
async def refresh_strategy(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """SSE 流：刷新所有股票的策略生成"""
    from bottleneck_hunter.watchlist.strategy_engine import refresh_strategy_all
    from bottleneck_hunter.watchlist.budget import BudgetTracker

    store = _user_store(user).for_market(market)
    budget = BudgetTracker(store)

    async def event_generator():
        async for evt in refresh_strategy_all(store, budget):
            if await request.is_disconnected():
                break
            yield {
                "event": evt.get("event", "progress"),
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }

    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.get("/strategy-summaries")
async def get_strategy_summaries(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """批量获取所有股票的最新策略信号（避免 N+1）"""
    store = _user_store(user).for_market(market)
    summaries = store.get_all_strategy_summaries()
    return {"summaries": summaries}


# ─────────────────────────────────────────────────────────────
# Watchlist CRUD
# ─────────────────────────────────────────────────────────────

@router.get("")
async def list_watchlist(tier: str | None = None, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entries = store.list_all(tier=tier)
    counts = store.count_by_tier()
    for e in entries:
        snap = store.get_latest_snapshot(e["ticker"])
        e["latest_snapshot"] = snap
    caps = store._effective_tier_caps()
    limits = {"total": sum(caps.values()), **caps}
    return {"entries": entries, "counts": counts, "total": len(entries), "limits": limits}


@router.post("")
async def add_to_watchlist(req: AddToWatchlistRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    try:
        data = req.model_dump()
        # 行业统一为细中文：用 company_profile 的 industry 映射，避免存入粗英文 "Technology"
        prof = store.get_company_profile(data.get("ticker", "")) or {}
        from bottleneck_hunter.watchlist.industry_zh import to_zh_sector
        zh = to_zh_sector(data.get("sector", ""), prof.get("industry", ""), prof.get("sector", ""))
        if zh:
            data["sector"] = zh
        entry_id = store.add(data)
        return {"id": entry_id, "status": "added"}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.put("/batch-tier")
async def batch_update_tier(req: Request, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    body = await req.json()
    ids = body.get("ids", [])
    tier = body.get("tier")
    if not ids or tier not in ("focus", "normal", "track"):
        raise HTTPException(status_code=400, detail="ids and valid tier required")
    updated = 0
    for eid in ids:
        if store.update(eid, tier=tier):
            updated += 1
    return {"status": "ok", "updated": updated}


@router.post("/batch-delete")
async def batch_delete(req: Request, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    body = await req.json()
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="ids required")
    removed = 0
    for eid in ids:
        if store.remove(eid):
            removed += 1
    return {"status": "ok", "removed": removed}


@router.get("/health")
async def pipeline_health(user: dict = Depends(get_current_user)):
    """返回管道状态 + 过期 ticker 列表。"""
    store = _user_store(user)
    statuses = store.get_pipeline_statuses()
    # 用户提示只算真有旧数据(>48h)的；刚添加/尚未抓取(NULL)的不算"超过48小时未更新"
    stale = store.get_stale_tickers(max_age_hours=48, include_never_fetched=False)
    return {"pipelines": statuses, "stale_tickers": stale}


# ─────────────────────────────────────────────────────────────
# Entry detail + sub-resources (parameterized /{entry_id})
# ─────────────────────────────────────────────────────────────

@router.get("/{entry_id}")
async def get_watchlist_entry(entry_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    entry["latest_snapshot"] = store.get_latest_snapshot(entry["ticker"])
    entry["recent_news"] = store.get_news(entry["ticker"], limit=5)
    return entry


@router.delete("/{entry_id}")
async def remove_from_watchlist(entry_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    removed = store.remove(entry_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"status": "removed"}


@router.patch("/{entry_id}")
async def update_watchlist_entry(entry_id: str, req: UpdateWatchlistRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if "tier" in fields:
        fields["tier"] = fields["tier"].value if hasattr(fields["tier"], "value") else fields["tier"]
    updated = store.update(entry_id, **fields)
    if not updated:
        raise HTTPException(status_code=404, detail="Entry not found or no changes")
    return {"status": "updated"}


@router.get("/{entry_id}/source-scorecard")
async def source_scorecard(entry_id: str, user: dict = Depends(get_current_user)):
    """Retrieve the original SupplierScorecard from the pipeline analysis."""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    aid = entry.get("source_analysis_id")
    if not aid or entry.get("source") != "phase4":
        return {"scorecard": None, "analysis_meta": None, "cross_validation": None, "rank": None}

    from bottleneck_hunter.dataflows.store import AnalysisStore
    analysis_store = AnalysisStore().for_user(user["sub"])
    record = analysis_store.get(aid)
    if not record:
        return {"scorecard": None, "analysis_meta": None, "cross_validation": None, "rank": None}

    result = record.get("result_json") or {}
    ticker = entry["ticker"]

    scorecard = None
    rank = None
    scorecards = result.get("supplier_scorecards") or []
    sorted_sc = sorted(scorecards, key=lambda s: (s.get("final", {}).get("final_score") or s.get("final_score") or 0), reverse=True)
    for i, sc in enumerate(sorted_sc):
        sc_ticker = sc.get("supplier", {}).get("ticker") or sc.get("ticker", "")
        if sc_ticker == ticker:
            scorecard = sc
            rank = i + 1
            break

    cv = None
    for v in (result.get("cross_validations") or []):
        if v.get("ticker") == ticker:
            cv = v
            break

    return {
        "scorecard": scorecard,
        "analysis_meta": {
            "sector": record.get("sector", ""),
            "end_product": record.get("end_product", ""),
            "market": record.get("market", ""),
            "created_at": record.get("created_at", ""),
            "total_scorecards": len(scorecards),
        },
        "cross_validation": cv,
        "rank": rank,
    }


@router.get("/{entry_id}/snapshots")
async def get_snapshots(entry_id: str, days: int = 90, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"snapshots": store.get_snapshots(entry["ticker"], days)}


@router.get("/{entry_id}/news")
async def get_news(entry_id: str, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"news": store.get_news(entry["ticker"], limit)}


@router.get("/{entry_id}/filings")
async def get_filings(entry_id: str, filing_type: str | None = None, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"filings": store.get_filings(entry["ticker"], filing_type, limit)}


@router.get("/{entry_id}/insider-trades")
async def get_insider_trades(entry_id: str, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"trades": store.get_insider_trades(entry["ticker"], limit)}


@router.get("/{entry_id}/options")
async def get_options(entry_id: str, limit: int = 10, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"options": store.get_options(entry["ticker"], limit)}


@router.get("/{entry_id}/earnings")
async def get_earnings(entry_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"earnings": store.get_earnings(entry["ticker"])}


@router.get("/{entry_id}/overview")
async def get_overview(entry_id: str, user: dict = Depends(get_current_user)):
    """聚合返回单只股票的概览数据（基本信息 tab 使用）。"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    ticker = entry["ticker"]
    return {
        "latest_snapshot": store.get_latest_snapshot(ticker),
        "profile": store.get_company_profile(ticker),
        "earnings": store.get_earnings(ticker),
    }


# ─────────────────────────────────────────────────────────────
# UZI Analysis — fixed path (history) BEFORE parameterized ({analysis_id})
# ─────────────────────────────────────────────────────────────

@router.get("/{entry_id}/uzi/history")
async def uzi_history(entry_id: str, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"history": store.get_uzi_history(entry_id, limit)}


@router.get("/{entry_id}/uzi/{analysis_id}")
async def uzi_result(entry_id: str, analysis_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    record = store.get_uzi_analysis(analysis_id)
    if not record or record["entry_id"] != entry_id:
        raise HTTPException(status_code=404, detail="Analysis not found")
    import json as _json
    if record.get("result_json"):
        try:
            record["result"] = _json.loads(record["result_json"])
        except _json.JSONDecodeError:
            record["result"] = None
        del record["result_json"]
    return record


@router.post("/{entry_id}/uzi/{analysis_type}")
async def uzi_trigger(entry_id: str, analysis_type: str, request: Request, user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.uzi_runner import ANALYSIS_TYPES, run_uzi_analysis

    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    if analysis_type not in ANALYSIS_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown type: {analysis_type}")

    import json as _json

    async def event_generator():
        async for evt in run_uzi_analysis(entry["ticker"], analysis_type, store, entry_id):
            if await request.is_disconnected():
                break
            yield {"event": evt.get("event", "progress"), "data": _json.dumps(evt, ensure_ascii=False)}

    return EventSourceResponse(with_notices(event_generator(), _sse))


# ─────────────────────────────────────────────────────────────
# Intelligence & Strategy endpoints
# ─────────────────────────────────────────────────────────────

@router.get("/{entry_id}/intelligence")
async def get_intelligence(entry_id: str, user: dict = Depends(get_current_user)):
    """获取最新情报"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    intel = store.get_latest_intelligence(entry_id)
    return {"intelligence": intel}


@router.get("/{entry_id}/intelligence/history")
async def get_intelligence_history(entry_id: str, limit: int = 10, user: dict = Depends(get_current_user)):
    """获取情报历史"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    history = store.get_intelligence_history(entry_id, limit)
    return {"history": history}


@router.get("/{entry_id}/strategy")
async def get_strategy(entry_id: str, user: dict = Depends(get_current_user)):
    """获取最新策略"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    strategy = store.get_latest_strategy(entry_id)
    return {"strategy": strategy}


@router.get("/{entry_id}/strategy/history")
async def get_strategy_history(entry_id: str, limit: int = 10, user: dict = Depends(get_current_user)):
    """获取策略历史"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    history = store.get_strategy_history(entry_id, limit)
    return {"history": history}

