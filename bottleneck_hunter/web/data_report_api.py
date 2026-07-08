"""外部数据获取报告 API — 挂载于 /api/data-report。

一个 /overview 端点打包：各数据源健康（付费源 probe 快照 + 免费 fetcher 熔断态 + hub provider 态）、
用量统计（今日/近7日按源聚合）、能力×市场覆盖矩阵。纯 REST（前端轮询，不用 SSE）。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from bottleneck_hunter.auth.dependencies import get_current_user, require_admin
from bottleneck_hunter.data_provider.data_source_catalog import (
    get_catalog,
    probe_source,
    resolve_data_source_key,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-report"])

_store = None
_auth_store = None


def set_stores(wl_store, auth_store) -> None:
    global _store, _auth_store
    _store = wl_store
    _auth_store = auth_store


def _wl():
    if _store is None:
        raise HTTPException(status_code=500, detail="Store 未初始化")
    return _store


# capability → 直连管线/免费源覆盖的市场（基线；hub provider 能力在 overview 里动态合并叠加）
_COVERAGE = {
    "quote": ["us_stock", "a_stock"], "daily": ["us_stock", "a_stock"],
    "earnings": ["us_stock", "a_stock"], "financials": ["us_stock", "a_stock"],
    "news": ["us_stock", "a_stock"],
    "sec": ["us_stock"], "institutional": ["us_stock"], "options": ["us_stock"],
    "notice": ["a_stock"], "smartmoney": ["us_stock", "a_stock"],
}
_ALL_MARKETS = ["us_stock", "a_stock", "hk_stock"]


@router.get("/overview")
async def overview(user: dict = Depends(get_current_user)):
    store = _wl()
    # 1) 数据源健康：付费源 probe 快照（pipeline_status ds_health:*）+ 已配置状态
    statuses = {s["pipeline_name"]: s for s in store.get_pipeline_statuses()}
    uid = user.get("sub", "")
    sources = []
    for src in get_catalog():
        sid = src["id"]
        health = statuses.get(f"ds_health:{sid}", {})
        configured = bool(resolve_data_source_key(sid, uid))
        sources.append({
            "id": sid, "name": src["name"], "testable": src.get("testable", False),
            "configured": configured,
            "health": health.get("last_status", "unknown"),
            "last_error": health.get("last_error", ""),
            "last_run_at": health.get("last_run_at", ""),
        })
    # 2) 用量统计
    usage_today = store.get_ds_stats_by_source(days=1)
    usage_7d = store.get_ds_stats_by_source(days=7)
    # 3) 管线状态（各 pipeline 上次运行，排除 ds_health:*）
    pipelines = [s for name, s in statuses.items() if not name.startswith("ds_health:")]
    # 4) 免费 fetcher 熔断态 + hub provider 态
    try:
        from bottleneck_hunter.data_provider import get_fetcher_manager
        manager = get_fetcher_manager().get_status()
    except Exception:  # noqa: BLE001
        manager = []
    try:
        from bottleneck_hunter.data_provider.hub import get_hub
        hub = get_hub().get_status()
    except Exception:  # noqa: BLE001
        hub = []
    # 5) 覆盖矩阵：基线（直连管线/免费源）+ hub provider 实际能力动态合并（insider 为死能力，暂不展示）
    cov = {cap: set(mks) for cap, mks in _COVERAGE.items()}
    for row in hub:
        for cap in row.get("capabilities", []):
            if cap == "insider":
                continue
            cov.setdefault(cap, set()).update(row.get("markets", []))
    coverage = [{"capability": cap, "markets": {m: (m in cov[cap]) for m in _ALL_MARKETS}}
                for cap in sorted(cov)]

    return {
        "sources": sources,
        "usage_today": usage_today,
        "usage_7d": usage_7d,
        "pipelines": pipelines,
        "manager": manager,
        "hub": hub,
        "coverage": coverage,
    }


@router.post("/probe")
async def manual_probe(user: dict = Depends(require_admin)):
    """管理员手动巡检所有 testable 付费源，落 pipeline_status 并返回结果。"""
    store = _wl()
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for src in get_catalog():
        sid = src["id"]
        if not src.get("testable"):
            continue
        key = resolve_data_source_key(sid)
        if not key:
            store.update_pipeline_status(f"ds_health:{sid}", last_status="idle",
                                         last_error="未配置 Key", last_run_at=now_iso)
            results.append({"id": sid, "ok": False, "msg": "未配置 Key"})
            continue
        ok, msg = await asyncio.to_thread(probe_source, sid, key, "")
        store.update_pipeline_status(f"ds_health:{sid}", last_status="success" if ok else "error",
                                     last_error="" if ok else msg[:200], last_run_at=now_iso)
        results.append({"id": sid, "ok": ok, "msg": msg})
    return {"results": results}
