"""API routes for BottleneckHunter web UI."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime
from typing import Optional

from pathlib import Path

from dotenv import dotenv_values, set_key
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.web.streaming import (
    stream_screening, run_cross_validation, run_refresh_suppliers, run_retry_bottleneck,
    stream_phase1, stream_phase2, stream_phase4, stream_roundtable,
)
from bottleneck_hunter.web import phase_cache
from bottleneck_hunter.web.streaming._common import _sse
from bottleneck_hunter.web.streaming._notice import with_notices
from bottleneck_hunter.chain.supplier_eval import FinalScorer

from bottleneck_hunter.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

ENV_PATH = Path.cwd() / ".env"

# ── 全局数据库实例 ─────────────────────────────────────
from bottleneck_hunter.dataflows.store import AnalysisStore

_store = AnalysisStore()


def _user_analysis_store(user: dict) -> AnalysisStore:
    """返回绑定当前用户的 AnalysisStore 实例。"""
    return _store.for_user(user["sub"])

PROVIDER_REGISTRY = [
    {"id": "openai",     "name": "OpenAI",       "env_var": "OPENAI_API_KEY"},
    {"id": "anthropic",  "name": "Anthropic",     "env_var": "ANTHROPIC_API_KEY"},
    {"id": "deepseek",   "name": "DeepSeek",      "env_var": "DEEPSEEK_API_KEY"},
    {"id": "google",     "name": "Google",         "env_var": "GOOGLE_API_KEY"},
    {"id": "qwen",       "name": "Qwen (通义)",    "env_var": "DASHSCOPE_API_KEY"},
    {"id": "glm",        "name": "GLM (智谱)",     "env_var": "ZHIPU_API_KEY"},
    {"id": "minimax",    "name": "MiniMax (海螺)",  "env_var": "MINIMAX_API_KEY"},
    {"id": "openrouter", "name": "OpenRouter",     "env_var": "OPENROUTER_API_KEY"},
    {"id": "siliconflow","name": "SiliconFlow",    "env_var": "SILICONFLOW_API_KEY"},
    {"id": "agnes",      "name": "Agnes AI",       "env_var": "AGNES_API_KEY"},
    {"id": "kimi",       "name": "Kimi (月之暗面)",  "env_var": "MOONSHOT_API_KEY"},
]


class ValidationModelConfig(BaseModel):
    provider: str
    model: str


class ScreenRequest(BaseModel):
    sector: str
    end_product: str
    max_depth: int = Field(default=3, ge=3, le=5)
    top_n: int = Field(default=5, ge=3, le=10)
    language: str = Field(default="zh")
    market: str = Field(default="us_stock")
    max_market_cap_yi: Optional[float] = 200
    max_suppliers: int = Field(default=20, ge=1, le=50)
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-5.5")
    enable_cross_validation: bool = False
    validation_models: list[ValidationModelConfig] = Field(default_factory=list)


@router.post("/screen")
async def screen(request: Request, config: ScreenRequest, user: dict = Depends(get_current_user)):
    store = _user_analysis_store(user)

    async def event_generator():
        async for event in stream_screening(config, store=store):
            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


class CrossValidateRequest(BaseModel):
    scorecards: list[dict]
    validation_models: list[ValidationModelConfig]
    language: str = "zh"


@router.post("/cross-validate")
async def cross_validate(req: CrossValidateRequest, user: dict = Depends(get_current_user)):
    """单独运行交叉验证，返回 SSE 事件流。"""
    async def event_generator():
        async for event in run_cross_validation(req.scorecards, req.validation_models, req.language):
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


class RefreshSuppliersRequest(BaseModel):
    bottleneck_reports: list[dict]
    market: str = "us_stock"
    max_market_cap_yi: Optional[float] = 200
    max_suppliers: int = 20
    language: str = "zh"
    provider: str = "openai"
    model: str = "gpt-5.5"


@router.post("/refresh-suppliers")
async def refresh_suppliers(req: RefreshSuppliersRequest, user: dict = Depends(get_current_user)):
    """重新运行供应商搜索+评估，返回 SSE 事件流。"""
    async def event_generator():
        async for event in run_refresh_suppliers(
            req.bottleneck_reports, req.market, req.max_market_cap_yi,
            req.max_suppliers, req.language, req.provider, req.model,
            user.get("sub", ""),
        ):
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


class RetryBottleneckRequest(BaseModel):
    chain: dict
    failed_nodes: list[dict]
    provider: str
    model: str
    language: str = "zh"


@router.post("/retry-bottleneck")
async def retry_bottleneck(req: RetryBottleneckRequest, user: dict = Depends(get_current_user)):
    """使用备选引擎对失败节点补充瓶颈分析，返回 SSE 事件流。"""
    async def event_generator():
        async for event in run_retry_bottleneck(
            req.chain, req.failed_nodes, req.provider, req.model, req.language,
        ):
            yield event

    return EventSourceResponse(with_notices(event_generator(), _sse))


class RefetchDataRequest(BaseModel):
    tickers: list[str]
    market: str = "us_stock"


@router.post("/refetch-data")
async def refetch_data(req: RefetchDataRequest, user: dict = Depends(get_current_user)):
    """为指定 ticker 重新拉取财务数据和聪明钱信号。"""
    from bottleneck_hunter.chain.models import SupplierInfo, MarketRegion
    from bottleneck_hunter.chain.financial_data import fetch_batch
    from bottleneck_hunter.chain.smart_money import track_batch as smart_money_batch

    region = MarketRegion.A_STOCK if req.market == "a_stock" else MarketRegion.US_STOCK
    suppliers = [SupplierInfo(name=t, ticker=t, market=region, sector="", description="") for t in req.tickers]

    (financial_map, fin_failed), (sm_map, sm_failed) = await asyncio.gather(
        fetch_batch(suppliers, user.get("sub", "")), smart_money_batch(suppliers))

    return {
        "financial": {k: v.model_dump() for k, v in financial_map.items()},
        "smart_money": {k: v.model_dump() for k, v in sm_map.items()},
        "still_failed": list(set(fin_failed + sm_failed)),
    }


# ── Phase API（4-Phase 分步流水线）──────────────────────────


class Phase1Request(BaseModel):
    sector: str
    end_product: str
    max_depth: int = Field(default=3, ge=3, le=5)
    top_n: int = Field(default=5, ge=3, le=10)
    language: str = "zh"
    provider: str = "openai"
    model: str = ""
    market: str = "us_stock"
    max_market_cap_yi: Optional[float] = 200


class ShortlistConfig(BaseModel):
    per_layer_top_n: int = Field(default=8, ge=1, le=20)
    layer_top_n: dict[str, int] | None = None
    min_overall_score: float = Field(default=0.0, ge=0, le=10)
    max_shortlist_count: int = Field(default=30, ge=1, le=100)


class Phase2Request(BaseModel):
    analysis_id: str
    shortlist_config: ShortlistConfig = Field(default_factory=ShortlistConfig)
    market: str = "us_stock"
    max_market_cap_yi: Optional[float] = 200
    max_suppliers: int = Field(default=20, ge=1, le=50)
    language: str = "zh"
    provider: str = "openai"
    model: str = ""


class ScoringConfig(BaseModel):
    quality_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    alpha_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    top_n: int = Field(default=5, ge=1, le=10)


class Phase3Request(BaseModel):
    analysis_id: str
    scoring_config: ScoringConfig = Field(default_factory=ScoringConfig)


class Phase4Request(BaseModel):
    analysis_id: str
    top_n: int = Field(default=10, ge=1, le=20)
    validation_models: list[ValidationModelConfig] = Field(default_factory=list)
    language: str = "zh"


@router.post("/phase1")
async def phase1(request: Request, req: Phase1Request, user: dict = Depends(get_current_user)):
    store = _user_analysis_store(user)

    async def event_generator():
        async for event in stream_phase1(
            sector=req.sector, end_product=req.end_product,
            max_depth=req.max_depth, top_n=req.top_n,
            language=req.language, provider=req.provider, model=req.model,
            market=req.market, max_market_cap_yi=req.max_market_cap_yi,
            store=store,
        ):
            if await request.is_disconnected():
                break
            yield event
    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.post("/phase2")
async def phase2(request: Request, req: Phase2Request, user: dict = Depends(get_current_user)):
    store = _user_analysis_store(user)

    async def event_generator():
        async for event in stream_phase2(
            analysis_id=req.analysis_id,
            per_layer_top_n=req.shortlist_config.per_layer_top_n,
            layer_top_n=req.shortlist_config.layer_top_n,
            min_overall_score=req.shortlist_config.min_overall_score,
            max_shortlist_count=req.shortlist_config.max_shortlist_count,
            market=req.market, max_market_cap_yi=req.max_market_cap_yi,
            max_suppliers=req.max_suppliers,
            language=req.language, provider=req.provider, model=req.model,
            store=store,
        ):
            if await request.is_disconnected():
                break
            yield event
    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.post("/phase3/score")
async def phase3_score(req: Phase3Request, user: dict = Depends(get_current_user)):
    """Phase 3: 即时重算最终评分（纯计算，无 LLM）。"""
    from bottleneck_hunter.chain.models import SupplierScorecard

    store = _user_analysis_store(user)

    p2 = phase_cache.get_phase(req.analysis_id, 2)
    if not p2:
        # 缓存未命中（容器重启/TTL过期/LRU淘汰）→ 从 DB 回读重建，避免逼用户重跑 Phase 2
        from bottleneck_hunter.web.phase_rehydrate import load_phase2_from_db
        p2 = load_phase2_from_db(store, req.analysis_id)
    if not p2:
        raise HTTPException(status_code=404, detail="Phase 2 数据未找到")

    scorecards = [SupplierScorecard(**d) for d in p2["scorecards"]]
    w_q = req.scoring_config.quality_weight
    w_a = req.scoring_config.alpha_weight
    top_n = req.scoring_config.top_n
    FinalScorer.score_all(scorecards, w_q=w_q, w_a=w_a)

    top_scorecards = scorecards[:top_n]
    ranked = []
    for i, sc in enumerate(top_scorecards, 1):
        key_factors = []
        if sc.alpha and sc.alpha.alpha_score >= 7:
            key_factors.append("高预期差")
        if sc.overall_score >= 7:
            key_factors.append("高质量")
        if sc.catalyst and sc.catalyst.urgency_score >= 7:
            key_factors.append("催化剂紧迫")
        if sc.smart_money and sc.smart_money.signal_direction == "bullish":
            key_factors.append("聪明钱看多")
        ranked.append({
            "rank": i,
            "scorecard": sc.model_dump(),
            "final": sc.final.model_dump() if sc.final else None,
            "key_factors": key_factors,
        })

    scoring_cfg = {"quality_weight": w_q, "alpha_weight": w_a, "top_n": top_n}
    # NaN/Inf（多来自缺失财务数据的 float 字段）会让 JSON 序列化 500。
    # 与 SSE 各阶段一致，在返回/入库前统一清洗为 null（复用 _common._sanitize）。
    from bottleneck_hunter.web.streaming._common import _sanitize
    phase3_data = _sanitize({
        "ranked_results": ranked,
        "scoring_config": scoring_cfg,
    })
    phase_cache.set_phase(req.analysis_id, 3, phase3_data)

    if store:
        try:
            store.update_suppliers(
                req.analysis_id, _sanitize([sc.model_dump() for sc in scorecards]),
                scoring_config=scoring_cfg,
            )
            store.set_completed_phases(req.analysis_id, 3)
        except Exception:
            logger.exception("Phase3 保存失败")

    return phase3_data


@router.post("/phase4")
async def phase4(request: Request, req: Phase4Request, user: dict = Depends(get_current_user)):
    store = _user_analysis_store(user)

    async def event_generator():
        vm = [{"provider": m.provider, "model": m.model} for m in req.validation_models]
        async for event in stream_phase4(
            analysis_id=req.analysis_id, top_n=req.top_n,
            validation_models=vm, language=req.language,
            store=store,
        ):
            if await request.is_disconnected():
                break
            yield event
    return EventSourceResponse(with_notices(event_generator(), _sse))


class MeetingRequest(BaseModel):
    analysis_id: str
    validation_models: list[ValidationModelConfig] = Field(default_factory=list)
    role_assignments: dict[str, ValidationModelConfig] | None = None
    language: str = "zh"


@router.post("/phase4/meeting")
async def phase4_meeting(request: Request, req: MeetingRequest, user: dict = Depends(get_current_user)):
    store = _user_analysis_store(user)

    async def event_generator():
        vm = [{"provider": m.provider, "model": m.model} for m in req.validation_models]
        ra = None
        if req.role_assignments:
            ra = {k: {"provider": v.provider, "model": v.model} for k, v in req.role_assignments.items()}
        async for event in stream_roundtable(
            analysis_id=req.analysis_id,
            validation_models=vm, language=req.language,
            role_assignments=ra,
            store=store,
        ):
            if await request.is_disconnected():
                break
            yield event
    return EventSourceResponse(with_notices(event_generator(), _sse))


@router.get("/phase/{analysis_id}/{phase_num}")
async def get_phase_data(analysis_id: str, phase_num: int, user: dict = Depends(get_current_user)):
    """获取已缓存的 Phase 结果。"""
    data = phase_cache.get_phase(analysis_id, phase_num)
    if data is None:
        # 缓存未命中 → 对有 DB 源的 Phase1/2 回读重建（Phase3/4 为派生，重跑上游即得）
        from bottleneck_hunter.web.phase_rehydrate import load_phase1_from_db, load_phase2_from_db
        store = _user_analysis_store(user)
        if phase_num == 1:
            data = load_phase1_from_db(store, analysis_id)
        elif phase_num == 2:
            data = load_phase2_from_db(store, analysis_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Phase {phase_num} 数据未找到")
    return data


@router.get("/hot-sectors")
async def hot_sectors(user: dict = Depends(get_current_user)):
    from bottleneck_hunter.chain.hot_sector import HotSectorDetector

    def _detect():
        detector = HotSectorDetector(top_n=20)
        return detector.detect()

    result = await asyncio.to_thread(_detect)

    sectors = []
    for s in result.all_ranked:
        sectors.append({
            "name": s.name,
            "sector_type": s.sector_type,
            "price_change_pct": s.price_change_pct,
            "turnover_rate": s.turnover_rate,
            "main_net_inflow": s.main_net_inflow,
            "volume": s.volume,
            "up_count": s.up_count,
            "down_count": s.down_count,
            "leader_stock": s.leader_stock,
            "composite_score": s.composite_score,
            "signal_count": s.signal_count,
        })

    emerging = []
    for s in result.emerging_themes:
        emerging.append({
            "name": s.name,
            "price_change_pct": s.price_change_pct,
            "main_net_inflow": s.main_net_inflow,
            "composite_score": s.composite_score,
        })

    return {"sectors": sectors, "emerging": emerging}


@router.get("/hot-recommendations")
async def hot_recommendations(user: dict = Depends(get_current_user)):
    """返回 top 5 推荐赛道（产业方向 + 终端产品），基于实时热门板块数据。"""
    from bottleneck_hunter.chain.hot_sector import HotSectorDetector

    def _recommend():
        detector = HotSectorDetector(top_n=20)
        return detector.recommend_sectors(top_n=5)

    results = await asyncio.to_thread(_recommend)
    return {
        "recommendations": [
            {
                "sector": r.sector,
                "end_product": r.end_product,
                "reason": r.reason,
                "score": r.score,
                "market": r.market,
                "source_board": r.source_board,
            }
            for r in results
        ]
    }


class HotScanRequest(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    top_n: int = Field(default=8, ge=3, le=12)


@router.post("/hot-scan")
async def hot_scan(req: HotScanRequest, user: dict = Depends(get_current_user)):
    """LLM 智能热点赛道推荐 — 可靠替代纯 AKShare 方案。"""
    from bottleneck_hunter.chain.hot_sector import llm_recommend_hot_sectors
    from bottleneck_hunter.llm_clients.fallback import begin_notices, drain_notices

    begin_notices()
    results = await llm_recommend_hot_sectors(req.provider, req.model, req.top_n)
    return {"recommendations": results, "fallback_notice": drain_notices()}


@router.get("/update-history")
async def update_history(user: dict = Depends(get_current_user)):
    """系统更新历史（最近 10 条，通俗版）。数据源：仓库根目录 UPDATE_HISTORY.json。"""
    # 稳健定位：优先仓库根（相对本文件，parents[2] = 仓库根），回退 cwd。
    # 勿只用 Path.cwd() —— 服务器工作目录随启动方式变化会读不到已提交的文件。
    candidates = [
        Path(__file__).resolve().parents[2] / "UPDATE_HISTORY.json",
        Path.cwd() / "UPDATE_HISTORY.json",
    ]
    items = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                items = parsed
                break
        except Exception as e:
            logger.warning("读取更新历史失败 (%s): %s", path, e)
    # 按日期倒序（容错：无 date 的排后），全部返回
    items.sort(key=lambda x: str(x.get("date", "")), reverse=True)
    return {"updates": items}


@router.get("/report")
async def download_report(path: str, user: dict = Depends(get_current_user)):
    """Download a generated report file."""
    report_path = Path(path)
    if not report_path.exists() or not report_path.suffix == ".md":
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/markdown",
        filename=report_path.name,
    )


# ── History API ──────────────────────────────────────────────


@router.get("/history")
async def list_history(user: dict = Depends(get_current_user)):
    """返回所有历史分析摘要（按时间倒序）。"""
    store = _user_analysis_store(user)
    return {"analyses": store.list_all()}


@router.get("/company-archive")
async def get_company_archive(ticker: str, user: dict = Depends(get_current_user)):
    """按 ticker 取企业持久化档案（含 scorecard 的简介+五维/预期差评分）。

    评选/入围(phase2/3)、反查(reverse)过的企业均有档案，观察池/决策中心据此直接展示"系统评分"，
    不再依赖易失的 source_analysis 反查。
    """
    store = _user_analysis_store(user)
    return {"archive": store.get_company_archive(ticker)}


@router.get("/history/{analysis_id}")
async def get_history(analysis_id: str, user: dict = Depends(get_current_user)):
    """返回完整分析结果（含 result_json）。"""
    store = _user_analysis_store(user)
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return record


@router.delete("/history/{analysis_id}")
async def delete_history(analysis_id: str, user: dict = Depends(get_current_user)):
    """删除一条历史分析记录。"""
    store = _user_analysis_store(user)
    deleted = store.delete(analysis_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"ok": True}


@router.patch("/history/{analysis_id}/phase-status")
async def update_phase_status(analysis_id: str, body: dict, user: dict = Depends(get_current_user)):
    """更新信号灯状态。"""
    store = _user_analysis_store(user)
    ps = body.get("phase_status", {})
    if store:
        store.update_phase_status(analysis_id, ps)
    return {"ok": True}


@router.post("/history/{analysis_id}/restore")
async def restore_history(analysis_id: str, user: dict = Depends(get_current_user)):
    """从历史记录恢复数据到 phase_cache，返回完整数据供前端渲染。"""
    store = _user_analysis_store(user)
    record = store.get(analysis_id)
    if not record:
        raise HTTPException(status_code=404, detail="Analysis not found")

    result = record.get("result_json", {})
    chain = result.get("chain")
    all_reports = result.get("bottleneck_reports", [])

    response = {
        "analysis_id": analysis_id,
        "seq_no": record.get("seq_no"),
        "sector": record.get("sector", ""),
        "end_product": record.get("end_product", ""),
        "market": record.get("market", "a_stock"),
        "provider": record.get("provider", ""),
        "model": record.get("model", ""),
        "max_depth": record.get("max_depth", 4),
        "top_n": record.get("top_n", 5),
        "max_market_cap_yi": record.get("max_market_cap_yi"),
        "completed_phases": record.get("completed_phases", 0),
        "created_at": record.get("created_at", ""),
        "run_count": record.get("run_count", 0),
        "_phase_status": result.get("_phase_status", {}),
        "phases": {},
    }

    if chain and all_reports:
        top_reports = sorted(all_reports, key=lambda r: r.get("overall_score", 0), reverse=True)[:record.get("top_n", 5)]
        phase1_data = {
            "chain": chain,
            "all_reports": all_reports,
            "top_reports": top_reports,
        }
        phase_cache.set_phase(analysis_id, 1, phase1_data)
        response["phases"]["1"] = phase1_data

    scorecards = result.get("supplier_scorecards", [])
    if scorecards:
        phase2_data = {
            "scorecards": scorecards,
            "config": {},
            "stats": {"total_searched": len(scorecards), "after_eval": len(scorecards), "after_filter": len(scorecards)},
        }
        phase_cache.set_phase(analysis_id, 2, phase2_data)
        response["phases"]["2"] = phase2_data

        # ── Phase 3: 从已保存的 scorecards 重建排名和图表数据 ──
        scoring_config = result.get("scoring_config", {"quality_weight": 0.5, "alpha_weight": 0.5})
        ranked = []
        for sc in scorecards:
            final = sc.get("final") or {}
            key_factors = []
            if (sc.get("alpha") or {}).get("alpha_score", 0) >= 7:
                key_factors.append("高预期差")
            if sc.get("overall_score", 0) >= 7:
                key_factors.append("高质量")
            if (sc.get("catalyst") or {}).get("urgency_score", 0) >= 7:
                key_factors.append("催化剂紧迫")
            sm = sc.get("smart_money") or {}
            if sm.get("signal_direction") == "bullish":
                key_factors.append("聪明钱看多")
            ranked.append({
                "rank": 0,
                "scorecard": sc,
                "final": final,
                "key_factors": key_factors,
            })
        ranked.sort(key=lambda r: (r.get("final") or {}).get("final_score", 0), reverse=True)
        top_n = scoring_config.get("top_n", 5)
        ranked = ranked[:top_n]
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        phase3_data = {"ranked_results": ranked, "scoring_config": scoring_config}
        phase_cache.set_phase(analysis_id, 3, phase3_data)
        response["phases"]["3"] = phase3_data

    cross_validations = result.get("cross_validations", [])
    if cross_validations:
        # ── Phase 4: 现为 FactCheck Gate，cross_validations 存的即 recommendations；兼容旧共识结构 ──
        recommendations = []
        for cv in cross_validations:
            ticker = cv.get("ticker", "")
            sc_match = next((s for s in scorecards if s.get("supplier", {}).get("ticker") == ticker), None)
            fc_final = (sc_match.get("final") or {}) if sc_match else {}
            consensus = cv.get("consensus_score", 0)  # 仅旧结构有
            pass_fail = cv.get("pass_fail") or (
                "pass" if consensus >= 7.5 else ("concern" if consensus >= 5 else "fail"))
            recommendations.append({
                "ticker": ticker,
                "name": cv.get("name") or cv.get("supplier_name", ""),
                "final_score": cv.get("final_score", fc_final.get("final_score", 0)),
                "credibility": cv.get("credibility", fc_final.get("credibility")),
                "recommendation": cv.get("recommendation", ""),
                "pass_fail": pass_fail,
            })
        phase4_data = {"validations": [], "recommendations": recommendations}
        phase_cache.set_phase(analysis_id, 4, phase4_data)
        response["phases"]["4"] = phase4_data

    ai_reports = result.get("ai_reports", {})
    if ai_reports:
        response["ai_reports"] = ai_reports

    meeting_result = result.get("meeting_result")
    if meeting_result:
        response["meeting_result"] = meeting_result

    def _sanitize(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    return JSONResponse(content=_sanitize(response))


class UpdateCvRequest(BaseModel):
    cross_validations: list[dict]


@router.patch("/history/{analysis_id}/cross-validation")
async def update_cross_validation(analysis_id: str, req: UpdateCvRequest, user: dict = Depends(get_current_user)):
    """更新指定分析记录的交叉验证结果。"""
    store = _user_analysis_store(user)
    updated = store.update_cross_validations(analysis_id, req.cross_validations)
    if not updated:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"ok": True}


class UpdateSuppliersRequest(BaseModel):
    supplier_scorecards: list[dict]
    cross_validations: list[dict] | None = None


@router.patch("/history/{analysis_id}/suppliers")
async def update_suppliers(analysis_id: str, req: UpdateSuppliersRequest, user: dict = Depends(get_current_user)):
    """更新指定分析记录的供应商评估和交叉验证结果。"""
    store = _user_analysis_store(user)
    updated = store.update_suppliers(
        analysis_id, req.supplier_scorecards, req.cross_validations
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"ok": True}


# ── AI 分析报告 API ──────────────────────────────────────────


class AiReportRequest(BaseModel):
    analysis_id: str
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    report_type: str = Field("comparison", description="comparison | chart_interp")
    chart_type: str = Field("", description="scatter | radar | bar | stack (for chart_interp)")
    force: bool = Field(False, description="强制重新生成，忽略缓存")


@router.get("/ai-report/{analysis_id}")
async def get_ai_reports(analysis_id: str, user: dict = Depends(get_current_user)):
    """读取已保存的所有 AI 评点。"""
    store = _user_analysis_store(user)
    return {"ai_reports": store.get_ai_reports(analysis_id)}


@router.post("/ai-report")
async def ai_report(req: AiReportRequest, user: dict = Depends(get_current_user)):
    """AI 生成横向对比报告或图表解读，返回 SSE 流。生成完毕后自动持久化。"""
    from bottleneck_hunter.chain.models import SupplierScorecard
    from bottleneck_hunter.llm_clients.factory import create_llm, get_llm_for_position
    from langchain_core.messages import SystemMessage, HumanMessage

    _user_store = _user_analysis_store(user)
    p2 = phase_cache.get_phase(req.analysis_id, 2)
    _db_record = None
    if (not p2 or not p2.get("scorecards")) and _user_store:
        _db_record = _user_store.get(req.analysis_id)
        if _db_record and _db_record.get("result_json", {}).get("supplier_scorecards"):
            p2 = {
                "scorecards": _db_record["result_json"]["supplier_scorecards"],
                "config": {},
                "stats": {"total_searched": len(_db_record["result_json"]["supplier_scorecards"])},
            }
            phase_cache.set_phase(req.analysis_id, 2, p2)
    if not p2 or not p2.get("scorecards"):
        raise HTTPException(status_code=404, detail="Phase 2 数据未找到")

    report_key = req.chart_type if req.report_type == "chart_interp" else "comparison"
    p3 = phase_cache.get_phase(req.analysis_id, 3)
    if not p3 and _user_store:
        if not _db_record:
            _db_record = _user_store.get(req.analysis_id)
        if _db_record:
            scoring_config = _db_record.get("result_json", {}).get("scoring_config", {"quality_weight": 0.5, "alpha_weight": 0.5})
            p3 = {"scoring_config": scoring_config}
            phase_cache.set_phase(req.analysis_id, 3, p3)
    current_scoring = (p3 or {}).get("scoring_config", {"quality_weight": 0.5, "alpha_weight": 0.5})

    if not req.force and _user_store:
        cached = _user_store.get_ai_reports(req.analysis_id).get(report_key)
        if cached and cached.get("scoring_config") == current_scoring:
            async def cached_generator():
                text = cached["text"]
                yield {"event": "done", "data": json.dumps({
                    "full_text": text, "cached": True,
                    "model": cached.get("model", ""),
                    "provider": cached.get("provider", ""),
                    "generated_at": cached.get("generated_at", ""),
                }, ensure_ascii=False)}
            return EventSourceResponse(cached_generator())

    scorecards = [SupplierScorecard(**d) for d in p2["scorecards"]]

    # ── 获取产业链上下文 ──
    sector_ctx = ""
    if _user_store:
        record = _user_store.get(req.analysis_id)
        if record:
            s = record.get("sector", "")
            ep = record.get("end_product", "")
            mkt = record.get("market", "a_stock")
            if s or ep:
                mkt_label = "A股" if mkt == "a_stock" else "美股"
                sector_ctx = f"本次分析的产业链：{s} → 终端产品: {ep}（{mkt_label}市场）\n"

    # ── 构建权重说明 ──
    w_q = current_scoring.get("quality_weight", 0.5)
    w_a = current_scoring.get("alpha_weight", 0.5)

    # ── 构建丰富的公司数据摘要 ──
    summary_lines = []
    for i, sc in enumerate(scorecards[:10], 1):
        a = sc.alpha
        cat = sc.catalyst
        moat = sc.moat
        snap = sc.financial_snapshot
        alpha_val = a.alpha_score if a else 0
        final_val = sc.final.final_score if sc.final else 0

        line = (
            f"{i}. {sc.supplier.name} ({sc.supplier.ticker})"
            f" | 所属瓶颈环节: {sc.bottleneck_node}"
        )
        # 五维评分明细
        line += (
            f"\n   质量分={sc.overall_score:.1f} "
            f"[市场地位={sc.market_position}, 客户验证={sc.customer_validation}, "
            f"产能={sc.capacity_status}, 财务={sc.financial_health}, 估值={sc.valuation}]"
        )
        # 护城河
        if moat:
            line += (
                f"\n   护城河={moat.overall_moat:.1f} "
                f"[专利={moat.patent_moat}, 转换成本={moat.switching_cost}, "
                f"产能壁垒={moat.capacity_lead_time}, 成本优势={moat.cost_advantage}]"
            )
            if moat.moat_reasoning:
                line += f" — {moat.moat_reasoning[:60]}"
        # Alpha 明细
        if a:
            line += f"\n   Alpha={alpha_val:.1f}"
            if a.reasoning:
                line += f" ({a.reasoning[:80]})"
        # 催化剂
        if cat and cat.events:
            evts = "; ".join(f"{e.description[:30]}(影响力{e.impact_score:.0f}/10)" for e in cat.events[:3])
            line += f"\n   催化剂: {evts} (整体紧迫度={cat.urgency_score:.1f}/10)"
        # 聪明钱
        sm = sc.smart_money
        if sm:
            line += f"\n   聪明钱: {sm.signal_direction}(分数={sm.smart_money_score:.1f})"
            if sm.details:
                line += f" — {'; '.join(sm.details[:2])}"
        # 财务快照
        if snap:
            fin_parts = []
            if snap.revenue_yi is not None:
                fin_parts.append(f"营收{snap.revenue_yi:.1f}亿")
            if snap.net_profit_yi is not None:
                fin_parts.append(f"净利{snap.net_profit_yi:.1f}亿")
            if snap.roe_pct is not None:
                fin_parts.append(f"ROE={snap.roe_pct:.1f}%")
            if snap.gross_margin_pct is not None:
                fin_parts.append(f"毛利率={snap.gross_margin_pct:.1f}%")
            if snap.consensus_pe is not None:
                fin_parts.append(f"一致预期PE={snap.consensus_pe:.1f}")
            if fin_parts:
                line += f"\n   财务: {', '.join(fin_parts)}"
        # 推荐分 + 优势/风险
        line += f"\n   推荐分={final_val:.2f}"
        if sc.strengths:
            line += f" | 优势: {'; '.join(sc.strengths[:3])}"
        if sc.weaknesses:
            line += f" | 风险: {'; '.join(sc.weaknesses[:2])}"
        summary_lines.append(line)

    company_summary = "\n\n".join(summary_lines)

    # ── 统一方法论背景 ──
    methodology = f"""{sector_ctx}
## 评分方法论

### 质量分（quality_score, 0-10）
LLM 对供应商在瓶颈环节中的竞争力评估，5 个维度等权平均（各占 70%），再加护城河得分（占 30%）：
- **市场地位** (market_position): 市占率、垄断/寡头地位
- **客户验证** (customer_validation): 大客户订单、验证状态
- **产能状况** (capacity_status): 产能利用率、扩产计划
- **财务健康** (financial_health): 营收增速、毛利率、现金流
- **估值水平** (valuation): PE/PB 相对行业均值
- **护城河** (moat): 专利壁垒、转换成本、产能壁垒、成本优势的综合

### 预期差/Alpha（alpha_score, 0-10）
衡量"市场尚未充分定价的程度"，核心公式：
  基础Alpha = 瓶颈重要度 × (1 - 市场关注度/10)
  最终Alpha = 基础Alpha × 催化剂紧迫度系数 + 趋势加分 + 聪明钱加分
市场关注度由 5 维加权构成：市值(15%) + 分析师覆盖(20%) + 成交量动量(25%) + 近3月涨幅(15%) + 机构持仓(25%)。
关注度越低 → 信息差越大 → Alpha 越高。催化剂和聪明钱信号可以进一步放大 Alpha。

### 推荐分（final_score, 0-10）
几何加权均值：final = quality^{w_q:.2f} × alpha^{w_a:.2f}
当前用户设定的权重：质量权重={w_q:.0%}，预期差权重={w_a:.0%}。
这意味着{'预期差（Alpha）对最终排名影响更大' if w_a > w_q else '质量分对最终排名影响更大' if w_q > w_a else '质量与预期差同等重要'}。"""

    # ── 构建公司名称列表（用于第三段个股小结） ──
    company_names = ", ".join(f"{sc.supplier.name}" for sc in scorecards[:10])

    # ── 构建图表专属或报告 prompt ──
    three_section_fmt = f"""请严格按以下三段格式输出（使用 Markdown 标题）：

## 一、图表概念说明
{{concept}}

## 二、横向对比分析
{{analysis}}

## 三、个股小结
请为数据中的每家公司各用 1-2 句话总结其在该图表中的表现特征和投资含义。
公司列表：{company_names}
格式示例：
- **公司A**: 一句话总结
- **公司B**: 一句话总结"""

    if req.report_type == "chart_interp":
        chart_prompts = {
            "scatter": f"""{three_section_fmt.format(
concept='''请介绍"质量分 vs 预期差散点图"的基本概念：
- X 轴表示质量分（0-10），即 LLM 对供应商在瓶颈环节竞争力的综合评估
- Y 轴表示预期差/Alpha（0-10），即市场尚未充分定价的程度
- 气泡大小对应推荐分高低（推荐分 = quality^{wq} × alpha^{wa} 几何均值）
- 四个象限的含义：右上=高质量+高预期差（最优）、左上=高Alpha低质量（投机型）、右下=高质量低Alpha（已充分定价）、左下=双低（规避区）'''.format(wq=w_q, wa=w_a),
analysis='''请结合方法论背景和数据，对散点分布进行系统解读：
1. 识别右上象限的"双高"标的，解释它们为何同时兼具质量和预期差
2. 指出左上象限（高Alpha低质量）的标的，评估其投机性和风险
3. 识别右下象限（高质量低Alpha）的标的，分析市场是否已充分定价
4. 发现异常离散点或聚类模式，揭示可能被忽视的投资机会
不要重复罗列原始数据，聚焦于投资洞察。''')}""",

            "radar": f"""{three_section_fmt.format(
concept='''请介绍"五维雷达对比图"的基本概念：
- 该雷达图展示排名前 5 家公司在 5 个质量维度上的对比
- 五个维度分别是：市场地位（市占率和行业话语权）、客户验证（下游大客户认可度）、产能状况（产能利用率和扩张能力）、财务健康（盈利能力和现金流）、估值水平（相对估值吸引力）
- 每个维度 0-10 分，由 LLM 基于公开信息评估
- 雷达覆盖面积越大代表综合竞争力越强；形状越均衡说明无明显短板''',
analysis='''请结合方法论背景和数据，对雷达图进行系统解读：
1. 哪家公司的雷达覆盖面积最大、最均衡？这说明什么竞争优势？
2. 哪些公司存在明显"短板维度"？这些短板是否构成投资风险？
3. 比较各公司的差异化优势维度（如 A 公司产能最强但估值最贵）
4. 结合实际财务数据判断，维度打分是否与基本面一致？
不要重复罗列原始数据，聚焦于竞争格局洞察。''')}""",

            "bar": f"""{three_section_fmt.format(
concept='''请介绍"评分因子横向对比柱状图"的基本概念：
- 该图并排展示每家公司的三个核心指标的柱状对比
- 质量分（quality_score）：LLM 评估的综合质量（5 维均值 × 0.7 + 护城河 × 0.3）
- 预期差（alpha_score）：市场尚未充分定价的程度，由市场关注度反向推导
- 推荐分（final_score）：质量分与预期差的几何加权均值（quality^{wq} × alpha^{wa}），当前权重设定下{weight_desc}
- 三根柱子的高低关系揭示每家公司的"质量-预期差"平衡状态'''.format(wq=w_q, wa=w_a, weight_desc='预期差影响更大' if w_a > w_q else ('质量分影响更大' if w_q > w_a else '两者同等重要')),
analysis='''请结合方法论背景和数据，对柱状对比进行系统解读：
1. 质量分最高与 Alpha 最高的是否是同一批公司？差异说明什么？
2. 推荐分排序是否出现"逆袭"（如质量排名不高但因 Alpha 大而推荐分靠前）？权重配置如何影响了排名？
3. 指出"质量分和 Alpha 双高"的最优标的
4. 哪些公司的三项指标最不协调（如质量很高但 Alpha 极低）？背后可能的原因是什么？
不要重复罗列原始数据，结合产业链逻辑提炼洞察。''')}""",

            "stack": f"""{three_section_fmt.format(
concept='''请介绍"Alpha 因子拆解堆叠图"的基本概念：
- 该堆叠柱状图将每家公司的 Alpha 预期差得分拆解为 4 个组成部分
- 基础Alpha（蓝色）：由"瓶颈重要度 × (1 - 市场关注度/10)"计算，反映纯信息差
- 催化剂加成（红色）：根据催化剂事件的紧迫度计算的乘数效应（越紧迫越高）
- 趋势加分（绿色）：财务趋势（营收/利润加速、毛利率扩张）带来的额外加分
- 聪明钱加分（黄色）：机构动向、大单信号等看多信号的加分
- 总高度 = 最终 Alpha 得分，堆叠结构揭示 Alpha 的来源和可持续性''',
analysis='''请结合方法论背景和数据，对堆叠图进行系统解读：
1. 哪些公司的 Alpha 主要来自"纯信息差"（基础 Alpha 占比大）？这类标的有什么特点？
2. 哪些公司依赖催化剂驱动？催化剂的具体内容和紧迫度如何？
3. 趋势加分和聪明钱加分显著的标的，是否有基本面支撑？
4. 对比 Alpha 总分相近但组成结构不同的公司，哪种结构更可靠、更可持续？
不要重复罗列原始数据，深入解读 Alpha 来源的可持续性。''')}""",
        }
        prompt = f"""{methodology}

## 公司数据

{company_summary}

{chart_prompts.get(req.chart_type, f"请对图表给出 3-5 句精炼解读，指出最值得关注的模式。")}"""
    else:
        prompt = f"""{methodology}

## 公司数据

{company_summary}

## 报告要求

请撰写一份横向对比分析报告（Markdown 格式），包含：
1. **综合排名概览** — 结合推荐分公式（quality^{w_q:.2f} × alpha^{w_a:.2f}），解释当前排名为何如此，权重配置如何影响了排序
2. **质量维度对比** — 基于 5 维评分明细，对比各公司在市场地位、客户验证、产能、财务、估值方面的优劣，结合实际财务数据佐证
3. **预期差（Alpha）分析** — 哪些公司信息差最大（关注度最低）、催化剂最紧迫、聪明钱信号最强？Alpha 主要来自哪个因子？
4. **护城河深度** — 基于 4 维护城河评分，对比各公司在专利壁垒、转换成本、产能壁垒、成本优势方面的差异
5. **风险提示** — 各公司主要风险因素和潜在陷阱，特别关注评分中的短板维度
6. **投资建议** — 分优先级推荐（首选/次选/观察），结合产业链瓶颈定位、质量/Alpha 平衡给出理由

语言简洁有力，避免笼统表述，多用具体数据和公司名称对比。"""

    system_prompt = (
        "你是一位资深产业链投研分析师，擅长从多维度横向对比上市公司投资价值。"
        "你深谙瓶颈选股方法论（从产业链拆解到瓶颈定位到供应商评估），"
        "能从评分数据中识别出市场尚未发现的投资机会。"
        "请基于提供的方法论背景和公司数据进行分析，确保你的解读体现对算法逻辑的理解。"
    )

    # provider 为空 = 主模型下拉"跟随顶栏配置" → 走角色/回退链解析，勿直接 create_llm('') 会 500
    if req.provider:
        llm = create_llm(req.provider, req.model)
        _provider, _model = req.provider, req.model
    else:
        llm, _provider, _model = get_llm_for_position(None)
        if llm is None:
            raise HTTPException(status_code=400, detail="未配置可用的 AI 模型，请在顶栏 AI 配置中心设置")
    _aid = req.analysis_id
    _sc = current_scoring

    async def event_generator():
        try:
            full_text = ""
            has_streamed = False
            try:
                async for chunk in llm.astream([
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=prompt),
                ]):
                    token = chunk.content if hasattr(chunk, "content") else str(chunk)
                    if token:
                        full_text += token
                        has_streamed = True
                        yield {"event": "chunk", "data": json.dumps({"text": token}, ensure_ascii=False)}
            except (NotImplementedError, AttributeError):
                response = await llm.ainvoke([
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=prompt),
                ])
                full_text = response.content.strip()
                has_streamed = False

            if not has_streamed and full_text:
                chunk_size = 200
                for start in range(0, len(full_text), chunk_size):
                    yield {"event": "chunk", "data": json.dumps({"text": full_text[start:start + chunk_size]}, ensure_ascii=False)}

            gen_at = datetime.now().isoformat(timespec="seconds")
            yield {"event": "done", "data": json.dumps({
                "full_text": full_text.strip(),
                "model": _model,
                "provider": _provider,
                "generated_at": gen_at,
            }, ensure_ascii=False)}

            if _user_store:
                try:
                    _user_store.update_ai_report(
                        _aid, report_key, full_text.strip(), _sc,
                        model=_model, provider=_provider, generated_at=gen_at,
                    )
                except Exception:
                    logger.exception("AI 评点持久化失败")
        except Exception as e:
            logger.exception("AI report generation failed")
            yield {"event": "error", "data": json.dumps({"message": str(e)}, ensure_ascii=False)}

    return EventSourceResponse(with_notices(event_generator(), _sse))


# ── Settings API ──────────────────────────────────────────────


def _mask_value(val: str | None, is_url: bool = False) -> str:
    if not val:
        return ""
    if is_url:
        return val
    if len(val) <= 8:
        return "***" + val[-2:]
    return "***" + val[-4:]


def _build_providers_response(user_id: str = "") -> list[dict]:
    """构建 providers 列表 —— 严格按当前用户判定配置状态，无任何全局 KEY 概念。"""
    # 当前用户已配置的 KEY hint
    user_keys: dict[str, str] = {}
    if user_id:
        try:
            from bottleneck_hunter.web.user_api import _store as _user_auth_store
            store = _user_auth_store()
            for k in store.get_user_api_keys(user_id):
                user_keys[k["provider"]] = k["key_hint"]
        except Exception:
            pass

    result = []
    for p in PROVIDER_REGISTRY:
        env_var = p["env_var"]
        is_url = p.get("is_url", False)
        has_user = p["id"] in user_keys

        configured = has_user
        masked = user_keys[p["id"]] if has_user else ""

        result.append({
            "id": p["id"],
            "name": p["name"],
            "env_var": env_var,
            "is_url": is_url,
            "configured": configured,
            "masked": masked,
            "source": "user" if has_user else "",  # 只可能是 user；无全局
            "has_global": False,                    # 严格隔离：不存在全局 KEY
        })
    return result


@router.get("/stock/{ticker}/kline")
async def stock_kline(ticker: str, market: str = "us_stock", user: dict = Depends(get_current_user)):
    from bottleneck_hunter.chain.financial_data import fetch_kline
    data = await fetch_kline(ticker, market)
    return data


@router.get("/settings")
async def get_settings(user: dict = Depends(get_current_user)):
    return {"providers": _build_providers_response(user.get("sub", ""))}


class SaveSettingsRequest(BaseModel):
    settings: dict[str, str]


def persist_provider_keys(user: dict, settings: dict[str, str]) -> int:
    """持久化 Provider API Key（严格按用户隔离，单一真源）。

    所有用户（含 admin）的 KEY 一律只加密写入用户级存储（user_api_keys 表）。
    **不再写 .env / os.environ**——杜绝任何全局 KEY，防止跨用户借用。

    settings: {ENV_VAR: value}。返回成功保存的数量。
    供 /api/settings 与 /api/ai-config/providers/keys 共用。
    """
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    allowed_vars = {p["env_var"] for p in PROVIDER_REGISTRY}
    env_to_provider = {p["env_var"]: p["id"] for p in PROVIDER_REGISTRY}
    user_id = user.get("sub", "")

    saved = 0
    for key, value in settings.items():
        if key not in allowed_vars:
            continue
        value = value.strip()
        provider_id = env_to_provider.get(key, "")
        if not provider_id or not user_id:
            continue

        # 空值 = 清除该用户对该 provider 的 KEY
        if not value:
            try:
                from bottleneck_hunter.web.user_api import _store as _get_auth
                _get_auth().delete_user_api_key(user_id, provider_id)
            except Exception as e:
                logger.warning(f"清除用户 KEY 失败 ({provider_id}): {e}")
            saved += 1
            continue

        try:
            from bottleneck_hunter.web.user_api import _store as _get_auth
            _get_auth().save_user_api_key(user_id, provider_id, encrypt(value), make_hint(value))
            saved += 1
        except Exception as e:
            logger.warning(f"保存用户 KEY 失败 ({provider_id}): {e}")

    return saved


@router.post("/settings")
async def save_settings(req: SaveSettingsRequest, user: dict = Depends(get_current_user)):
    """保存 API KEY。

    所有用户的 KEY 都保存到用户级加密存储。
    admin 额外写入 .env 作为全局 fallback。
    """
    persist_provider_keys(user, req.settings)
    return {"ok": True, "providers": _build_providers_response(user.get("sub", ""))}


# ── 测试 Provider 连通性 ────────────────────────────────


@router.post("/test-providers")
async def test_providers(user: dict = Depends(get_current_user)):
    """测试所有已配置 Key 的 Provider 能否正常调用 LLM。"""
    from langchain_core.messages import HumanMessage
    from bottleneck_hunter.llm_clients.factory import create_llm, resolve_provider_model

    user_id = user.get("sub", "")

    configured = []
    for p in PROVIDER_REGISTRY:
        is_url = p.get("is_url", False)
        # 严格隔离：只认当前用户自己的 KEY，无全局兜底
        user_key = None
        if user_id:
            try:
                from bottleneck_hunter.web.user_api import resolve_user_api_key
                user_key = resolve_user_api_key(user_id, p["id"])
            except Exception:
                pass

        if not user_key:
            continue
        model = resolve_provider_model(p["id"], user_id)
        if not model:
            continue
        configured.append({
            "id": p["id"], "name": p["name"], "model": model,
            "is_url": is_url, "api_key": user_key,
        })

    async def _test_one(info: dict) -> dict:
        pid = info["id"]
        try:
            llm = create_llm(pid, info["model"], api_key=info.get("api_key"), with_fallback=False)
            await asyncio.wait_for(
                llm.ainvoke([HumanMessage(content="hi")]),
                timeout=60,
            )
            return {"id": pid, "name": info["name"], "success": True}
        except asyncio.TimeoutError:
            return {"id": pid, "name": info["name"], "success": False, "error": "请求超时（60s）"}
        except Exception as e:
            err_msg = str(e)
            if len(err_msg) > 120:
                err_msg = err_msg[:120] + "..."
            return {"id": pid, "name": info["name"], "success": False, "error": err_msg}

    results = await asyncio.gather(*[_test_one(c) for c in configured])
    return {"results": list(results)}


class ValidateModelsRequest(BaseModel):
    main_provider: str
    main_model: str
    cv_models: list[ValidationModelConfig] = Field(default_factory=list)
    market: str = "us_stock"


async def _test_llm(provider: str, model: str, label: str) -> dict:
    """测试单个 LLM 模型是否可用。供 validate_models 和 meeting preflight 复用。"""
    from langchain_core.messages import HumanMessage
    from bottleneck_hunter.llm_clients.factory import create_llm
    try:
        llm = create_llm(provider, model, with_fallback=False)
        await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="hi")]),
            timeout=30,
        )
        return {"label": label, "provider": provider, "model": model, "type": "llm", "success": True}
    except asyncio.TimeoutError:
        return {"label": label, "provider": provider, "model": model, "type": "llm", "success": False, "error": "超时(30s)"}
    except Exception as e:
        err = str(e)[:120]
        return {"label": label, "provider": provider, "model": model, "type": "llm", "success": False, "error": err}


class PreflightRequest(BaseModel):
    models: list[ValidationModelConfig]


@router.post("/meeting/preflight")
async def meeting_preflight(req: PreflightRequest, user: dict = Depends(get_current_user)):
    """会前连通性预检：并行测试所有指定模型。"""
    seen = set()
    tasks = []
    for m in req.models:
        key = f"{m.provider}::{m.model}"
        if key in seen:
            continue
        seen.add(key)
        tasks.append(_test_llm(m.provider, m.model, f"{m.provider}/{m.model}"))
    results = await asyncio.gather(*tasks)
    return {"results": list(results)}


@router.post("/validate-models")
async def validate_models(req: ValidateModelsRequest, user: dict = Depends(get_current_user)):
    """验证指定的主模型、交叉验证模型和财务数据接口是否可用。"""

    async def _test_financial_api(market: str) -> dict:
        if market == "a_stock":
            try:
                import akshare as ak
                df = await asyncio.to_thread(
                    lambda: ak.stock_financial_abstract_ths(symbol="600519", indicator="按报告期")
                )
                ok = df is not None and not df.empty
                return {"label": "A股财务数据", "type": "financial", "api": "akshare", "success": ok,
                        "error": "" if ok else "返回空数据"}
            except ImportError:
                return {"label": "A股财务数据", "type": "financial", "api": "akshare", "success": False, "error": "akshare 未安装"}
            except Exception as e:
                return {"label": "A股财务数据", "type": "financial", "api": "akshare", "success": False, "error": str(e)[:120]}
        else:
            try:
                import yfinance as yf
                stock = await asyncio.to_thread(lambda: yf.Ticker("AAPL"))
                info = await asyncio.to_thread(lambda: stock.info or {})
                ok = bool(info.get("shortName"))
                return {"label": "美股财务数据", "type": "financial", "api": "yfinance", "success": ok,
                        "error": "" if ok else "返回空数据"}
            except ImportError:
                return {"label": "美股财务数据", "type": "financial", "api": "yfinance", "success": False, "error": "yfinance 未安装"}
            except Exception as e:
                return {"label": "美股财务数据", "type": "financial", "api": "yfinance", "success": False, "error": str(e)[:120]}

    tasks = [_test_llm(req.main_provider, req.main_model, "主分析模型")]
    for i, cv in enumerate(req.cv_models):
        tasks.append(_test_llm(cv.provider, cv.model, f"交叉验证#{i+1}"))
    tasks.append(_test_financial_api(req.market))

    results = await asyncio.gather(*tasks)
    return {"results": list(results)}
