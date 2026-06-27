"""认证 API 路由：登录 / 注册 / 登出 / 当前用户。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.auth.jwt_utils import clear_auth_cookie, create_token, set_auth_cookie
from bottleneck_hunter.auth.models import ChangePasswordRequest, LoginRequest, RegisterRequest, UserInfo
from bottleneck_hunter.auth.store import AuthStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 模块级引用，由 app.py lifespan 注入
_auth_store: AuthStore | None = None


def set_auth_store(store: AuthStore):
    global _auth_store
    _auth_store = store


def _store() -> AuthStore:
    if _auth_store is None:
        raise RuntimeError("AuthStore 未初始化")
    return _auth_store


# ── 登录 ──────────────────────────────────────────────────

@router.post("/login")
async def login(req: LoginRequest, response: Response):
    store = _store()
    user = store.get_user_by_username(req.username)
    if not user or not store.verify_password(user, req.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被冻结")

    token = create_token(user.id, user.username, user.role)
    set_auth_cookie(response, token)
    store.update_last_login(user.id)
    logger.info(f"用户登录: {user.username}")
    return {"ok": True, "user": UserInfo(**user.model_dump()).model_dump()}


# ── 注册 ──────────────────────────────────────────────────

@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    store = _store()

    # 检查注册权限
    registration_open = store.is_registration_open()
    invite_valid = False
    if req.invite_code:
        ic = store.validate_invite_code(req.invite_code)
        if ic:
            invite_valid = True

    if not registration_open and not invite_valid:
        raise HTTPException(status_code=403, detail="注册未开放，请提供有效邀请码")

    # 检查用户名重复
    if store.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")

    # 获取默认上限
    default_limit = int(store.get_config("default_watchlist_limit", "24"))

    # 创建用户
    user = store.create_user(
        username=req.username,
        password=req.password,
        display_name=req.display_name or req.username,
        watchlist_limit=default_limit,
    )

    # 消费邀请码
    if invite_valid and req.invite_code:
        store.consume_invite_code(req.invite_code, user.id)

    token = create_token(user.id, user.username, user.role)
    set_auth_cookie(response, token)
    store.update_last_login(user.id)
    logger.info(f"新用户注册: {user.username}")
    return {"ok": True, "user": UserInfo(**user.model_dump()).model_dump()}


# ── 登出 ──────────────────────────────────────────────────

@router.post("/logout")
async def logout(response: Response):
    clear_auth_cookie(response)
    return {"ok": True}


# ── 当前用户 ──────────────────────────────────────────────

@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    store = _store()
    db_user = store.get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(status_code=401, detail="用户不存在")
    return UserInfo(**db_user.model_dump()).model_dump()


# ── 修改密码 ─────────────────────────────────────────────

@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)):
    store = _store()
    db_user = store.get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not store.verify_password(db_user, req.old_password):
        raise HTTPException(status_code=400, detail="原密码错误")
    store.change_password(db_user.id, req.new_password)
    return {"ok": True}
