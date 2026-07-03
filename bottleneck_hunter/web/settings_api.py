"""自动更新设置 API — 挂载于 /api/settings

- GET  /auto-update           当前用户的开关/阈值 + 各定时任务状态 + 全局时间表（只读）
- PATCH /auto-update          保存当前用户的开关/阈值
- POST /auto-update/run/{cat} 立即触发某分类的自动更新（后台）
- GET  /schedule              (管理员) 全局时间表 + 全局总开关
- PATCH /schedule             (管理员) 改全局时间表/总开关 → 免重启重排任务
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user, require_admin
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.store_budget import AUTO_UPDATE_DEFAULTS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])

_store: WatchlistStore | None = None
_auth_store = None


def set_stores(wl_store: WatchlistStore, auth_store) -> None:
    global _store, _auth_store
    _store = wl_store
    _auth_store = auth_store


def _user_store(user: dict) -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="Store 未初始化")
    return _store.for_user(user.get("sub", ""))


# 分类展示标签
CATEGORY_LABELS = {
    "watchlist_data": "观察池数据（行情/新闻/公告/期权/机构）",
    "daily_decision": "日常决策（L1-L4 + 投委会）",
    "weekly_strategy": "每周策略重生成",
    "auto_review": "自动复盘与偏好学习",
    "catalyst": "催化剂扫描",
    "full_refresh": "周期性全量刷新",
}


# ── 用户设置 ────────────────────────────────────────────────
class AutoUpdatePatch(BaseModel):
    master_enabled: bool | None = None
    watchlist_data: bool | None = None
    daily_decision: bool | None = None
    weekly_strategy: bool | None = None
    auto_review: bool | None = None
    catalyst: bool | None = None
    full_refresh: bool | None = None
    stale_threshold_hours: int | None = Field(None, ge=1, le=720)


@router.get("/auto-update")
async def get_auto_update(user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.scheduler import get_job_statuses, list_job_categories
    from bottleneck_hunter.watchlist.schedule_config import get_global_schedule, is_global_enabled

    store = _user_store(user)
    cfg = store.get_auto_update_config()
    cat_map = list_job_categories()
    jobs = get_job_statuses()
    for j in jobs:
        j["category"] = cat_map.get(j["id"], "")
    return {
        "config": cfg,
        "category_labels": CATEGORY_LABELS,
        "jobs": jobs,
        "pipelines": store.get_pipeline_statuses(),
        "global_enabled": is_global_enabled(_auth_store),   # 只读展示
        "global_schedule": get_global_schedule(_auth_store),  # 只读展示（用户看时间但不能改）
        "is_admin": user.get("role") == "admin",
    }


@router.patch("/auto-update")
async def patch_auto_update(req: AutoUpdatePatch, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    updates = req.model_dump(exclude_none=True)
    for key, val in updates.items():
        if key == "stale_threshold_hours":
            store.set_auto_update_config(key, str(int(val)))
        elif key in AUTO_UPDATE_DEFAULTS:
            store.set_auto_update_config(key, "1" if val else "0")
    return {"status": "ok", "config": store.get_auto_update_config()}


# category → 立即执行时触发的 job 协程工厂（全局 job，内部按用户门控）
def _run_map(category: str):
    from bottleneck_hunter.watchlist import scheduler as S
    return {
        "watchlist_data": [lambda: S.job_price_update("us_stock"), lambda: S.job_price_update("a_stock"),
                           lambda: S.job_daily_scan("us_stock"), lambda: S.job_daily_scan("a_stock"),
                           lambda: S.job_stale_refresh()],
        "daily_decision": [lambda: S.job_daily_decision("us_stock"), lambda: S.job_daily_decision("a_stock")],
        "weekly_strategy": [lambda: S.job_weekly_strategy("us_stock"), lambda: S.job_weekly_strategy("a_stock")],
        "auto_review": [lambda: S.job_auto_review("us_stock"), lambda: S.job_auto_review("a_stock")],
        "catalyst": [lambda: S.job_catalyst_scan()],
        "full_refresh": [lambda: S.job_full_refresh("us_stock"), lambda: S.job_full_refresh("a_stock")],
    }.get(category)


@router.post("/auto-update/run/{category}")
async def run_now(category: str, user: dict = Depends(get_current_user)):
    """立即触发某分类的自动更新（后台执行，遵守各用户开关）。"""
    factories = _run_map(category)
    if not factories:
        raise HTTPException(status_code=400, detail=f"未知分类: {category}")

    async def _run():
        for f in factories:
            try:
                await f()
            except Exception:
                logger.exception("手动触发 %s 失败", category)

    asyncio.create_task(_run())
    return {"status": "triggered", "category": category}


# ── 管理员：全局时间表 ──────────────────────────────────────
class SchedulePatch(BaseModel):
    global_enabled: bool | None = None
    schedule: dict | None = None   # {job_id: {hour, minute, day_of_week, interval_hours}}


@router.get("/schedule")
async def get_schedule(admin: dict = Depends(require_admin)):
    from bottleneck_hunter.watchlist.schedule_config import get_global_schedule, is_global_enabled
    from bottleneck_hunter.watchlist.scheduler import list_job_categories
    return {
        "global_enabled": is_global_enabled(_auth_store),
        "schedule": get_global_schedule(_auth_store),
        "categories": list_job_categories(),
    }


@router.patch("/schedule")
async def patch_schedule(req: SchedulePatch, admin: dict = Depends(require_admin)):
    from bottleneck_hunter.watchlist.schedule_config import set_global_schedule, set_global_enabled
    from bottleneck_hunter.watchlist.scheduler import reschedule_all_from_config
    if req.global_enabled is not None:
        set_global_enabled(_auth_store, req.global_enabled)
    if req.schedule:
        set_global_schedule(_auth_store, req.schedule)
    # 改完立即重排（免重启）
    try:
        reschedule_all_from_config()
    except Exception as e:
        logger.warning("重排任务失败: %s", e)
    from bottleneck_hunter.watchlist.schedule_config import get_global_schedule, is_global_enabled
    return {"status": "ok", "global_enabled": is_global_enabled(_auth_store),
            "schedule": get_global_schedule(_auth_store)}
