"""反向分析 API 路由 — 挂载在 /api/reverse。

- POST /analyze        反向分析单只标的（SSE 流）
- GET  /list           反向分析列表（按用户+市场）
- POST /cross-analyze  对选中的反向分析企业做多模型交叉验证（复用 Phase4，SSE 流）
- GET  /{id}           单条完整记录（含 result_json）
- DELETE /{id}         删除一条
加入观察池复用 POST /api/watchlist，无需新端点。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.web.api import _user_analysis_store
from bottleneck_hunter.web.streaming._common import _sanitize
from bottleneck_hunter.web.streaming.reverse import stream_reverse_analysis
from bottleneck_hunter.web.streaming._common import _sse
from bottleneck_hunter.web.streaming._notice import with_notices
from bottleneck_hunter.web import refresh_guard

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reverse"])

async def _release_after(gk, gen):
    """透传 gen 事件，结束(含断开/异常)时释放并发闸。"""
    try:
        async for e in gen:
            yield e
    finally:
        refresh_guard.release(gk)


_store: WatchlistStore | None = None


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def _user_store(user: dict) -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="WatchlistStore not initialized")
    return _store.for_user(user["sub"])


# ─────────────────────────────────────────────────────────
class ReverseAnalyzeRequest(BaseModel):
    ticker: str
    market: str = "us_stock"
    language: str = "zh"
    provider: str = ""
    model: str = ""
    owner_analysis_id: str = ""


class ValidationModelConfig(BaseModel):
    provider: str
    model: str


class ReverseCrossRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    market: str = "us_stock"
    validation_models: list[ValidationModelConfig] = Field(default_factory=list)
    language: str = "zh"


# ── 静态路由（必须在 /{analysis_id} 之前） ──────────────

@router.post("/analyze")
async def reverse_analyze(request: Request, req: ReverseAnalyzeRequest,
                          user: dict = Depends(get_current_user)):
    analysis_store = _user_analysis_store(user)
    wl_store = _user_store(user).for_market(req.market)
    _gk = f"reverse:{user['sub']}"
    if not refresh_guard.acquire(_gk):
        async def _busy():
            yield _sse("error", step="init", message="已有反向分析在进行中，请等其完成后再试")
        return EventSourceResponse(with_notices(_busy(), _sse))

    async def event_generator():
        async for event in stream_reverse_analysis(
            ticker=req.ticker, market=req.market, language=req.language,
            provider=req.provider, model=req.model,
            analysis_store=analysis_store, watchlist_store=wl_store,
            user_id=user["sub"], owner_analysis_id=req.owner_analysis_id,
        ):
            if await request.is_disconnected():
                break
            yield event
    return EventSourceResponse(with_notices(_release_after(_gk, event_generator()), _sse))


@router.get("/list")
async def reverse_list(market: str = "us_stock", owner_analysis_id: str = "",
                       user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"records": _sanitize(
        store.list_reverse_analyses(owner_analysis_id=owner_analysis_id or None))}


@router.post("/cross-analyze")
async def reverse_cross_analyze(request: Request, req: ReverseCrossRequest,
                                user: dict = Depends(get_current_user)):
    from bottleneck_hunter.chain.cross_validation import CrossValidator
    from bottleneck_hunter.chain.models import SupplierScorecard
    from bottleneck_hunter.llm_clients.factory import get_models_for_role
    from bottleneck_hunter.web.streaming._common import _sse

    store = _user_store(user).for_market(req.market)
    vm = [{"provider": m.provider, "model": m.model} for m in req.validation_models]
    # 未显式指定时，自动用用户在 AI 配置中为「交叉验证(pipeline_cross_val)」选的模型
    if not vm:
        vm = [{"provider": p, "model": m}
              for (_, p, m) in get_models_for_role("pipeline_cross_val", user_id=user["sub"])]

    # 加载选中记录的 scorecard
    scorecards = []
    for rid in req.ids:
        rec = store.get_reverse_analysis(rid)
        if rec and rec.get("result_json"):
            try:
                scorecards.append(SupplierScorecard(**rec["result_json"]))
            except Exception:
                logger.warning("反向交叉分析：解析 scorecard 失败 id=%s", rid)

    _gk = f"reverse:{user['sub']}"
    if not refresh_guard.acquire(_gk):
        async def _busy():
            yield _sse("error", step="init", message="已有反向分析在进行中，请等其完成后再试")
        return EventSourceResponse(with_notices(_busy(), _sse))

    async def event_generator():
        if not scorecards:
            yield _sse("error", step="init", message="没有可交叉验证的企业")
            return
        if not vm:
            yield _sse("error", step="init", message="未配置验证模型")
            return
        yield _sse("step_start", step="cross_validate", index=0,
                   message=f"正在对 {len(scorecards)} 家企业进行交叉验证...")
        try:
            validator = CrossValidator(validation_models=vm, language=req.language)
            validations = await validator.validate_all(scorecards)
        except Exception as e:
            logger.exception("反向交叉分析失败")
            yield _sse("error", step="cross_validate", message=str(e))
            return
        recommendations = []
        for cv in validations:
            sc = next((s for s in scorecards if s.supplier.ticker == cv.ticker), None)
            recommendations.append({
                "ticker": cv.ticker, "name": cv.supplier_name,
                "final_score": sc.final.final_score if sc and sc.final else (sc.overall_score if sc else 0),
                "consensus": cv.consensus_score,
                "pass_fail": "pass" if cv.consensus_score >= 7.5 else ("concern" if cv.consensus_score >= 5 else "fail"),
            })
        yield _sse("reverse_cross_complete",
                   validations=[v.model_dump() for v in validations],
                   recommendations=recommendations,
                   ranked_results=[{"supplier": s.supplier.model_dump(),
                                    "ticker": s.supplier.ticker,
                                    "final_score": s.final.final_score if s.final else s.overall_score,
                                    "bottleneck_node": s.bottleneck_node} for s in scorecards])
    return EventSourceResponse(with_notices(_release_after(_gk, event_generator()), _sse))


# ── 参数化路由 ───────────────────────────────────────────

@router.get("/{analysis_id}")
async def reverse_get(analysis_id: str, market: str = "us_stock",
                      user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    rec = store.get_reverse_analysis(analysis_id)
    if not rec:
        raise HTTPException(status_code=404, detail="记录不存在")
    return _sanitize(rec)


@router.delete("/{analysis_id}")
async def reverse_delete(analysis_id: str, market: str = "us_stock",
                         user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    if not store.delete_reverse_analysis(analysis_id):
        raise HTTPException(status_code=404, detail="记录不存在")
    return {"status": "removed"}
