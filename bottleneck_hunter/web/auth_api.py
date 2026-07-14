"""认证 API 路由：登录 / 注册（邮箱验证）/ 登出 / 当前用户 / 账户管理。"""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.auth.email_sender import resolve_smtp_config, send_verification_email
from bottleneck_hunter.auth.jwt_utils import clear_auth_cookie, create_token, set_auth_cookie
from bottleneck_hunter.auth.models import (
    ChangePasswordRequest,
    ConfirmEmailChangeRequest,
    LoginRequest,
    RegisterRequest,
    RequestEmailChangeRequest,
    ResendCodeRequest,
    UserInfo,
    VerifyRegistrationRequest,
)
from bottleneck_hunter.auth.store import AuthStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 模块级引用，由 app.py lifespan 注入
_auth_store: AuthStore | None = None
_wl_store = None

RESEND_COOLDOWN_SECONDS = 60


def set_auth_store(store: AuthStore):
    global _auth_store
    _auth_store = store


def set_wl_store(store):
    """注入 WatchlistStore，用于账户面板展示观察池数量。"""
    global _wl_store
    _wl_store = store


def _store() -> AuthStore:
    if _auth_store is None:
        raise RuntimeError("AuthStore 未初始化")
    return _auth_store


def _gen_code() -> str:
    """生成 6 位数字验证码。"""
    return f"{secrets.randbelow(1000000):06d}"


def _smtp_config() -> dict:
    """解析生效的 SMTP 配置（管理后台配置优先，回退环境变量）。"""
    return resolve_smtp_config(_auth_store)


def _watchlist_count(user_id: str) -> int:
    if _wl_store is None:
        return 0
    try:
        counts = _wl_store.for_user(user_id).count_by_tier()
        return sum(counts.values()) if isinstance(counts, dict) else 0
    except Exception:
        logger.warning("获取观察池数量失败", exc_info=True)
        return 0


def _watchlist_count_by_market(user_id: str) -> dict[str, int]:
    """分市场观察池数量（美股/A股），供账户小徽标分市场展示。"""
    if _wl_store is None:
        return {"us_stock": 0, "a_stock": 0}
    try:
        us = _wl_store.for_user(user_id)
        return {m: sum(us.for_market(m).count_by_tier().values())
                for m in ("us_stock", "a_stock")}
    except Exception:
        logger.warning("获取分市场观察池数量失败", exc_info=True)
        return {"us_stock": 0, "a_stock": 0}


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
    from bottleneck_hunter.web.admin_events import notify_admins
    notify_admins("login", f"{user.username} 登录", username=user.username)
    return {"ok": True, "user": UserInfo(**user.model_dump()).model_dump()}


# ── 注册 ──────────────────────────────────────────────────

@router.post("/register")
async def register(req: RegisterRequest, response: Response):
    """第一阶段：校验邀请码 + 唯一性 → 发验证码到邮箱。不建号、不发 cookie。"""
    store = _store()

    # 检查注册权限（开放注册 或 有效邀请码）
    invite_valid = False
    if req.invite_code:
        invite_valid = store.validate_invite_code(req.invite_code) is not None
    if not store.is_registration_open() and not invite_valid:
        raise HTTPException(status_code=403, detail="注册未开放，请提供有效邀请码")

    # 唯一性检查
    if store.get_user_by_username(req.username):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if store.get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="该邮箱已被注册")

    # 生成验证码 + 预哈希密码，存 pending payload（不含明文密码）
    # 分档比例在注册时从全局默认快照进用户自身，此后 admin 改全局默认不影响该用户
    default_limit = int(store.get_config("default_watchlist_limit", "24"))
    default_focus = float(store.get_config("watchlist_tier_focus_pct", "0.25"))
    default_normal = float(store.get_config("watchlist_tier_normal_pct", "0.25"))
    code = _gen_code()
    payload = {
        "username": req.username,
        "display_name": req.display_name or req.username,
        "email": req.email,
        "password_hash": AuthStore.hash_password(req.password),
        "invite_code": req.invite_code,
        "watchlist_limit": default_limit,
        "watchlist_focus_pct": default_focus,
        "watchlist_normal_pct": default_normal,
    }
    store.create_verification(req.email, code, "register", payload)

    sent = await asyncio.to_thread(send_verification_email, req.email, code, "register", _smtp_config())
    if not sent:
        raise HTTPException(status_code=502, detail="验证码邮件发送失败，请稍后重试")
    logger.info("注册验证码已发送: %s", req.email)
    return {"ok": True, "pending": True, "email": req.email}


@router.post("/verify-registration")
async def verify_registration(req: VerifyRegistrationRequest, response: Response):
    """第二阶段：校验验证码 → 建号 → 消费邀请码 → 登录。"""
    store = _store()
    ok, msg, payload = store.verify_code(req.email, "register", req.code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    # 二次唯一性检查（防止并发/等待期间被占用）
    if store.get_user_by_username(payload.get("username", "")):
        raise HTTPException(status_code=409, detail="用户名已被占用，请重新注册")
    if store.get_user_by_email(payload.get("email", "")):
        raise HTTPException(status_code=409, detail="该邮箱已被注册，请重新注册")

    user = store.create_user(
        username=payload["username"],
        display_name=payload.get("display_name", ""),
        email=payload.get("email", ""),
        password_hash=payload["password_hash"],
        watchlist_limit=int(payload.get("watchlist_limit", 24)),
        focus_pct=float(payload.get("watchlist_focus_pct", 0.25)),
        normal_pct=float(payload.get("watchlist_normal_pct", 0.25)),
    )
    invite = payload.get("invite_code")
    if invite:
        store.consume_invite_code(invite, user.id)

    token = create_token(user.id, user.username, user.role)
    set_auth_cookie(response, token)
    store.update_last_login(user.id)
    logger.info("新用户注册完成: %s", user.username)
    return {"ok": True, "user": UserInfo(**user.model_dump()).model_dump()}


@router.post("/resend-code")
async def resend_code(req: ResendCodeRequest):
    """重发验证码（60s 冷却）。仅在已有 pending 记录时可用。"""
    store = _store()
    age = store.get_verification_age_seconds(req.email, req.purpose)
    if age is None:
        raise HTTPException(status_code=400, detail="没有待验证的请求，请重新发起")
    if age < RESEND_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail=f"请 {int(RESEND_COOLDOWN_SECONDS - age)} 秒后再试")
    # 复用现有 payload，仅换新码
    _, _, payload = _peek_payload(store, req.email, req.purpose)
    code = _gen_code()
    store.create_verification(req.email, code, req.purpose, payload)
    sent = await asyncio.to_thread(send_verification_email, req.email, code, req.purpose, _smtp_config())
    if not sent:
        raise HTTPException(status_code=502, detail="验证码邮件发送失败，请稍后重试")
    return {"ok": True}


def _peek_payload(store: AuthStore, email: str, purpose: str) -> tuple[bool, str, dict]:
    """读取但不消费当前验证码的 payload（供重发复用）。"""
    import json
    with store._conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM email_verifications WHERE email = ? AND purpose = ?",
            (email, purpose),
        ).fetchone()
    return (True, "", json.loads(row["payload_json"] or "{}")) if row else (False, "", {})


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
    info = UserInfo(**db_user.model_dump()).model_dump()
    info["watchlist_count"] = _watchlist_count(db_user.id)
    info["watchlist_count_by_market"] = _watchlist_count_by_market(db_user.id)
    return info


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


# ── 修改邮箱（需验证新地址）─────────────────────────────

@router.post("/request-email-change")
async def request_email_change(req: RequestEmailChangeRequest, user: dict = Depends(get_current_user)):
    store = _store()
    db_user = store.get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not store.verify_password(db_user, req.password):
        raise HTTPException(status_code=400, detail="密码错误")
    existing = store.get_user_by_email(req.new_email)
    if existing and existing.id != db_user.id:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账户使用")

    code = _gen_code()
    store.create_verification(req.new_email, code, "change_email",
                              {"user_id": db_user.id, "new_email": req.new_email})
    sent = await asyncio.to_thread(send_verification_email, req.new_email, code, "change_email", _smtp_config())
    if not sent:
        raise HTTPException(status_code=502, detail="验证码邮件发送失败，请稍后重试")
    return {"ok": True, "email": req.new_email}


@router.post("/confirm-email-change")
async def confirm_email_change(req: ConfirmEmailChangeRequest, user: dict = Depends(get_current_user)):
    store = _store()
    db_user = store.get_user_by_id(user["sub"])
    if not db_user:
        raise HTTPException(status_code=401, detail="用户不存在")
    # change_email 的验证码以「新邮箱」为 key；从该用户的 pending 记录里找回
    import json
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT email, payload_json FROM email_verifications WHERE purpose = 'change_email'",
        ).fetchall()
    target_email = None
    for r in rows:
        try:
            if json.loads(r["payload_json"] or "{}").get("user_id") == db_user.id:
                target_email = r["email"]
                break
        except (ValueError, TypeError):
            continue
    if not target_email:
        raise HTTPException(status_code=400, detail="没有待验证的邮箱变更请求")
    ok, msg, payload = store.verify_code(target_email, "change_email", req.code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if payload.get("user_id") != db_user.id:
        raise HTTPException(status_code=403, detail="验证信息不匹配")
    store.update_email(db_user.id, payload["new_email"])
    logger.info("用户 %s 邮箱已更新", db_user.username)
    return {"ok": True, "email": payload["new_email"]}
