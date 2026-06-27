"""Admin API router — /api/admin 端点（仅管理员可访问）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_auth_store = None
_wl_store = None


def set_stores(auth_store, wl_store=None):
    global _auth_store, _wl_store
    _auth_store = auth_store
    _wl_store = wl_store


def _auth():
    if _auth_store is None:
        raise HTTPException(status_code=500, detail="AuthStore not initialized")
    return _auth_store


def _require_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ── 请求模型 ──────────────────────────────────────────

class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    watchlist_limit: int | None = None
    display_name: str | None = None


class CreateInviteCodesRequest(BaseModel):
    count: int = Field(default=5, ge=1, le=50)
    expires_days: int = Field(default=30, ge=0, le=365)


class UpdateConfigRequest(BaseModel):
    open_registration: bool | None = None
    default_watchlist_limit: int | None = None


# ── 用户管理 ──────────────────────────────────────────

@router.get("/users")
async def list_users(user: dict = Depends(_require_admin)):
    store = _auth()
    users = store.list_users()
    result = []
    for u in users:
        info = u.model_dump() if hasattr(u, "model_dump") else dict(u)
        info.pop("password_hash", None)
        info.pop("settings_json", None)
        # 统计每用户的 watchlist 和 analysis 数
        if _wl_store:
            try:
                us = _wl_store.for_user(info["id"])
                info["watchlist_count"] = len(us.list_all())
            except Exception:
                info["watchlist_count"] = 0
        result.append(info)
    return {"users": result}


@router.get("/users/{user_id}")
async def get_user(user_id: str, user: dict = Depends(_require_admin)):
    store = _auth()
    u = store.get_user_by_id(user_id)
    if not u:
        raise HTTPException(status_code=404, detail="用户不存在")
    info = u.model_dump()
    info.pop("password_hash", None)
    if _wl_store:
        try:
            us = _wl_store.for_user(user_id)
            info["watchlist_count"] = len(us.list_all())
        except Exception:
            info["watchlist_count"] = 0
    # API KEY 统计
    keys = store.get_user_api_keys(user_id)
    info["api_key_count"] = len(keys)
    return info


@router.patch("/users/{user_id}")
async def update_user(user_id: str, req: UpdateUserRequest, user: dict = Depends(_require_admin)):
    store = _auth()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="无修改内容")
    store.update_user(user_id, **fields)
    logger.info(f"管理员更新用户 {target.username}: {fields}")
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, user: dict = Depends(_require_admin)):
    store = _auth()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.role == "admin" and target.id == user["sub"]:
        raise HTTPException(status_code=400, detail="不能删除自己")

    # 清理用户数据
    if _wl_store:
        try:
            us = _wl_store.for_user(user_id)
            for entry in us.list_all():
                us.remove(entry["id"])
        except Exception as e:
            logger.warning(f"清理用户 watchlist 数据失败: {e}")

    try:
        from bottleneck_hunter.dataflows.store import AnalysisStore
        analysis_store = AnalysisStore().for_user(user_id)
        for a in analysis_store.list():
            analysis_store.delete(a["id"])
    except Exception as e:
        logger.warning(f"清理用户 analysis 数据失败: {e}")

    store.delete_all_user_api_keys(user_id)
    store.delete_user(user_id)
    logger.info(f"管理员删除用户 {target.username}")
    return {"ok": True}


@router.post("/users/{user_id}/freeze")
async def freeze_user(user_id: str, user: dict = Depends(_require_admin)):
    store = _auth()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == user["sub"]:
        raise HTTPException(status_code=400, detail="不能冻结自己")
    store.update_user(user_id, is_active=False)
    logger.info(f"管理员冻结用户 {target.username}")
    return {"ok": True}


@router.post("/users/{user_id}/unfreeze")
async def unfreeze_user(user_id: str, user: dict = Depends(_require_admin)):
    store = _auth()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    store.update_user(user_id, is_active=True)
    logger.info(f"管理员解冻用户 {target.username}")
    return {"ok": True}


# ── 邀请码管理 ────────────────────────────────────────

@router.get("/invite-codes")
async def list_invite_codes(user: dict = Depends(_require_admin)):
    store = _auth()
    codes = store.list_invite_codes()
    return {"codes": [c.model_dump() for c in codes]}


@router.post("/invite-codes")
async def create_invite_codes(req: CreateInviteCodesRequest, user: dict = Depends(_require_admin)):
    store = _auth()
    codes = store.create_invite_codes(req.count, user["sub"], req.expires_days)
    logger.info(f"管理员生成 {req.count} 个邀请码")
    return {"codes": codes}


@router.delete("/invite-codes/{code}")
async def revoke_invite_code(code: str, user: dict = Depends(_require_admin)):
    store = _auth()
    store.revoke_invite_code(code)
    return {"ok": True}


# ── 系统配置 ──────────────────────────────────────────

@router.get("/config")
async def get_config(user: dict = Depends(_require_admin)):
    store = _auth()
    return {
        "open_registration": store.get_config("open_registration", "0") == "1",
        "default_watchlist_limit": int(store.get_config("default_watchlist_limit", "24")),
    }


@router.patch("/config")
async def update_config(req: UpdateConfigRequest, user: dict = Depends(_require_admin)):
    store = _auth()
    if req.open_registration is not None:
        store.set_config("open_registration", "1" if req.open_registration else "0")
    if req.default_watchlist_limit is not None:
        store.set_config("default_watchlist_limit", str(req.default_watchlist_limit))
    return await get_config(user)


# ── 统计 ──────────────────────────────────────────────

@router.get("/stats")
async def get_stats(user: dict = Depends(_require_admin)):
    store = _auth()
    total_users = store.count_users()
    total_invites = len(store.list_invite_codes())

    total_watchlist = 0
    total_analyses = 0
    if _wl_store:
        try:
            import sqlite3
            conn = sqlite3.connect(str(_wl_store._db_path))
            total_watchlist = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
            conn.close()
        except Exception:
            pass
    try:
        from bottleneck_hunter.dataflows.store import AnalysisStore
        import sqlite3
        conn = sqlite3.connect(str(AnalysisStore().db_path))
        total_analyses = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        conn.close()
    except Exception:
        pass

    return {
        "total_users": total_users,
        "total_invites": total_invites,
        "total_watchlist": total_watchlist,
        "total_analyses": total_analyses,
    }
