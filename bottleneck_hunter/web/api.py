"""API routes for BottleneckHunter web UI."""

from __future__ import annotations

import logging
import os
from typing import Optional

from pathlib import Path

from dotenv import dotenv_values, set_key
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.web.streaming import stream_screening

logger = logging.getLogger(__name__)

router = APIRouter()

ENV_PATH = Path.cwd() / ".env"

# ── 全局数据库实例 ─────────────────────────────────────
from bottleneck_hunter.dataflows.store import AnalysisStore

_store = AnalysisStore()

PROVIDER_REGISTRY = [
    {"id": "openai",     "name": "OpenAI",       "env_var": "OPENAI_API_KEY"},
    {"id": "anthropic",  "name": "Anthropic",     "env_var": "ANTHROPIC_API_KEY"},
    {"id": "deepseek",   "name": "DeepSeek",      "env_var": "DEEPSEEK_API_KEY"},
    {"id": "google",     "name": "Google",         "env_var": "GOOGLE_API_KEY"},
    {"id": "qwen",       "name": "Qwen (通义)",    "env_var": "DASHSCOPE_API_KEY"},
    {"id": "glm",        "name": "GLM (智谱)",     "env_var": "ZHIPU_API_KEY"},
    {"id": "openrouter", "name": "OpenRouter",     "env_var": "OPENROUTER_API_KEY"},
    {"id": "siliconflow","name": "SiliconFlow",    "env_var": "SILICONFLOW_API_KEY"},
    {"id": "ollama",     "name": "Ollama (本地)",   "env_var": "OLLAMA_BASE_URL", "is_url": True},
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
    market: str = Field(default="a_stock")
    max_market_cap_yi: Optional[float] = 200
    max_suppliers: int = Field(default=20, ge=1, le=50)
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-5.5")
    enable_cross_validation: bool = False
    validation_models: list[ValidationModelConfig] = Field(default_factory=list)


@router.post("/screen")
async def screen(request: Request, config: ScreenRequest):
    async def event_generator():
        async for event in stream_screening(config, store=_store):
            if await request.is_disconnected():
                break
            yield event

    return EventSourceResponse(event_generator())


@router.get("/hot-sectors")
async def hot_sectors():
    import asyncio
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


@router.get("/report")
async def download_report(path: str):
    """Download a generated report file."""
    report_path = Path(path)
    if not report_path.exists() or not report_path.suffix == ".md":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(
        path=str(report_path),
        media_type="text/markdown",
        filename=report_path.name,
    )


# ── History API ──────────────────────────────────────────────


@router.get("/history")
async def list_history():
    """返回所有历史分析摘要（按时间倒序）。"""
    return {"analyses": _store.list_all()}


@router.get("/history/{analysis_id}")
async def get_history(analysis_id: str):
    """返回完整分析结果（含 result_json）。"""
    record = _store.get(analysis_id)
    if not record:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Analysis not found")
    return record


@router.delete("/history/{analysis_id}")
async def delete_history(analysis_id: str):
    """删除一条历史分析记录。"""
    deleted = _store.delete(analysis_id)
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Analysis not found")
    return {"ok": True}


# ── Settings API ──────────────────────────────────────────────


def _mask_value(val: str | None, is_url: bool = False) -> str:
    if not val:
        return ""
    if is_url:
        return val
    if len(val) <= 8:
        return "***" + val[-2:]
    return "***" + val[-4:]


def _build_providers_response() -> list[dict]:
    env_vals = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    result = []
    for p in PROVIDER_REGISTRY:
        env_var = p["env_var"]
        is_url = p.get("is_url", False)
        raw = os.environ.get(env_var, "") or env_vals.get(env_var, "")
        result.append({
            "id": p["id"],
            "name": p["name"],
            "env_var": env_var,
            "is_url": is_url,
            "configured": bool(raw),
            "masked": _mask_value(raw, is_url),
        })
    return result


@router.get("/settings")
async def get_settings():
    return {"providers": _build_providers_response()}


class SaveSettingsRequest(BaseModel):
    settings: dict[str, str]


@router.post("/settings")
async def save_settings(req: SaveSettingsRequest):
    allowed_vars = {p["env_var"] for p in PROVIDER_REGISTRY}
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")

    for key, value in req.settings.items():
        if key not in allowed_vars:
            continue
        value = value.strip()
        if not value:
            continue
        set_key(str(ENV_PATH), key, value)
        os.environ[key] = value

    return {"ok": True, "providers": _build_providers_response()}
