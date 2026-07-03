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
    default_watchlist_limit: int | None = Field(default=None, ge=1, le=500)
    tier_focus_pct: float | None = Field(default=None, ge=0, le=1)
    tier_normal_pct: float | None = Field(default=None, ge=0, le=1)


class UpdateSmtpConfigRequest(BaseModel):
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, max_length=512)  # 空/省略=保持原密码
    sender: str | None = Field(default=None, max_length=255)
    use_tls: bool | None = None


class SmtpTestRequest(BaseModel):
    to_email: str = Field(..., min_length=5, max_length=128)


# ── 用户管理 ──────────────────────────────────────────

@router.get("/users")
async def list_users(user: dict = Depends(_require_admin)):
    store = _auth()
    users = store.list_users()
    from bottleneck_hunter.dataflows.store import AnalysisStore
    ana = AnalysisStore()
    result = []
    for u in users:
        info = u.model_dump() if hasattr(u, "model_dump") else dict(u)
        info.pop("password_hash", None)
        info.pop("settings_json", None)
        uid = info["id"]
        # 轻量计数（明细在 /overview 按需拉，避免用户多时 N+1 卡顿）
        info["watchlist_count"] = 0
        info["analysis_count"] = 0
        info["ai_config_count"] = 0
        if _wl_store:
            try:
                us = _wl_store.for_user(uid)
                info["watchlist_count"] = len(us.list_all())
                info["ai_config_count"] = len(us.get_role_configs(user_id=uid))
            except Exception:
                pass
        try:
            info["analysis_count"] = len(ana.for_user(uid).list_all())
        except Exception:
            pass
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
        for a in analysis_store.list_all():
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


# ── 用户数据总览 + AI 配置拷贝 ─────────────────────────

@router.get("/users/{user_id}/overview")
async def get_user_overview(user_id: str, user: dict = Depends(_require_admin)):
    """聚合单用户核心数据：产业链分析 / 观察池 / 分市场账户 / 分市场持仓 / AI配置。"""
    store = _auth()
    target = store.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    from bottleneck_hunter.dataflows.store import AnalysisStore

    analyses = []
    try:
        analyses = AnalysisStore().for_user(user_id).list_all()
    except Exception:
        logger.warning("读取用户 %s 分析记录失败", user_id, exc_info=True)

    watchlist = {"count_by_tier": {}, "items": []}
    accounts: list[dict] = []
    positions: list[dict] = []
    ai_config: list[dict] = []

    if _wl_store:
        us = _wl_store.for_user(user_id)
        try:
            watchlist["count_by_tier"] = us.count_by_tier()
            watchlist["items"] = [
                {"ticker": e.get("ticker"), "tier": e.get("tier"),
                 "sector": e.get("sector"), "company_name": e.get("company_name")}
                for e in us.list_all()
            ]
        except Exception:
            logger.warning("读取用户 %s 观察池失败", user_id, exc_info=True)

        try:
            for acct in us.list_sim_accounts(user_id):
                mkt = acct.get("market") or "us_stock"
                accounts.append({
                    "market": mkt, "name": acct.get("name"),
                    "total_equity": acct.get("total_equity"),
                    "total_return_pct": acct.get("total_return_pct"),
                    "cash_balance": acct.get("cash_balance"),
                    "win_rate": acct.get("win_rate"),
                    "total_trades": acct.get("total_trades"),
                })
                try:
                    pos = us.for_market(mkt).get_sim_positions(acct.get("id"))
                    positions.append({"market": mkt, "items": [
                        {"ticker": p.get("ticker"), "shares": p.get("shares"),
                         "avg_cost": p.get("avg_cost"), "current_price": p.get("current_price"),
                         "market_value": p.get("market_value"),
                         "unrealized_pnl": p.get("unrealized_pnl"),
                         "weight_pct": p.get("weight_pct")}
                        for p in pos]})
                except Exception:
                    logger.warning("读取用户 %s %s 持仓失败", user_id, mkt, exc_info=True)
        except Exception:
            logger.warning("读取用户 %s 账户失败", user_id, exc_info=True)

        try:
            ai_config = [
                {"role_key": c.get("role_key"), "role_label": c.get("role_label"),
                 "provider": c.get("provider"), "model": c.get("model"),
                 "slot_index": c.get("slot_index")}
                for c in us.get_role_configs(user_id=user_id)
            ]
        except Exception:
            logger.warning("读取用户 %s AI配置失败", user_id, exc_info=True)

    return {
        "user": {"id": target.id, "username": target.username,
                 "display_name": target.display_name, "role": target.role},
        "analyses": analyses,
        "watchlist": watchlist,
        "accounts": accounts,
        "positions": positions,
        "ai_config": ai_config,
    }


class CopyAiConfigReq(BaseModel):
    source_user_id: str
    role_keys: list[str] | None = None  # None = 全部


@router.post("/ai-config/copy-to-me")
async def copy_ai_config_to_me(req: CopyAiConfigReq, user: dict = Depends(_require_admin)):
    """把源用户选定角色的 AI 配置拷进管理员自己的账户（冲突覆盖，slot0 同步进程内 env）。"""
    if not _wl_store:
        raise HTTPException(status_code=500, detail="Store 未初始化")
    admin_uid = user["sub"]
    if req.source_user_id == admin_uid:
        raise HTTPException(status_code=400, detail="源用户不能是你自己")

    src = _wl_store.get_role_configs(user_id=req.source_user_id)
    if req.role_keys:
        wanted = set(req.role_keys)
        src = [c for c in src if c.get("role_key") in wanted]
    if not src:
        raise HTTPException(status_code=400, detail="源用户无可拷贝的 AI 配置")

    import os
    copied, roles = 0, set()
    for c in src:
        _wl_store.upsert_role_config(
            role_key=c["role_key"], slot_index=c.get("slot_index", 0),
            provider=c["provider"], model=c["model"],
            role_label=c.get("role_label", ""), role_group=c.get("role_group", ""),
            user_id=admin_uid,
        )
        copied += 1
        roles.add(c["role_key"])

    # provider 可用性提示：管理员未配 key 的 provider（拷贝成功但运行时会失败）
    missing: list[str] = []
    try:
        from bottleneck_hunter.web.ai_config_api import _build_providers_list
        available = {p["id"] for p in _build_providers_list()}
        missing = sorted({c["provider"] for c in src if c["provider"] not in available})
    except Exception:
        logger.debug("provider 可用性检查失败", exc_info=True)

    logger.info("管理员从 %s 拷贝 %d 条 AI 配置", req.source_user_id, copied)
    return {"ok": True, "copied": copied, "roles": sorted(roles), "missing_provider": missing}


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
    from bottleneck_hunter.watchlist.tier_limits import (
        DEFAULT_TOTAL, DEFAULT_FOCUS_PCT, DEFAULT_NORMAL_PCT, derive_tier_caps,
    )
    store = _auth()
    total = int(store.get_config("default_watchlist_limit", str(DEFAULT_TOTAL)))
    focus_pct = float(store.get_config("watchlist_tier_focus_pct", str(DEFAULT_FOCUS_PCT)))
    normal_pct = float(store.get_config("watchlist_tier_normal_pct", str(DEFAULT_NORMAL_PCT)))
    return {
        "open_registration": store.get_config("open_registration", "0") == "1",
        "default_watchlist_limit": total,
        "tier_focus_pct": focus_pct,
        "tier_normal_pct": normal_pct,
        # 按默认上限预览三档容量，便于前端直观展示
        "tier_caps_preview": derive_tier_caps(total, focus_pct, normal_pct),
    }


@router.patch("/config")
async def update_config(req: UpdateConfigRequest, user: dict = Depends(_require_admin)):
    store = _auth()
    if req.open_registration is not None:
        store.set_config("open_registration", "1" if req.open_registration else "0")
    if req.default_watchlist_limit is not None:
        store.set_config("default_watchlist_limit", str(req.default_watchlist_limit))
    # 比例：focus + normal 必须 < 1（给 track 留余量）
    if req.tier_focus_pct is not None or req.tier_normal_pct is not None:
        focus = req.tier_focus_pct if req.tier_focus_pct is not None else float(store.get_config("watchlist_tier_focus_pct", "0.25"))
        normal = req.tier_normal_pct if req.tier_normal_pct is not None else float(store.get_config("watchlist_tier_normal_pct", "0.25"))
        if focus + normal >= 1.0:
            raise HTTPException(status_code=400, detail="重点 + 一般 比例之和必须小于 100%（需给跟踪档留余量）")
        if req.tier_focus_pct is not None:
            store.set_config("watchlist_tier_focus_pct", str(req.tier_focus_pct))
        if req.tier_normal_pct is not None:
            store.set_config("watchlist_tier_normal_pct", str(req.tier_normal_pct))
    return await get_config(user)


# ── SMTP 邮件配置 ─────────────────────────────────────

@router.get("/smtp-config")
async def get_smtp_config(user: dict = Depends(_require_admin)):
    """返回当前 SMTP 配置（不含密码明文，仅提示是否已设置及来源）。"""
    from bottleneck_hunter.auth.email_sender import resolve_smtp_config
    store = _auth()
    cfg = resolve_smtp_config(store)
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "sender": cfg["from"],
        "use_tls": cfg["use_tls"],
        "password_set": bool(cfg["password"]),
        "configured": bool(cfg["host"]),
        "source": cfg["source"],  # db=后台配置 / env=环境变量兜底
    }


@router.patch("/smtp-config")
async def update_smtp_config(req: UpdateSmtpConfigRequest, user: dict = Depends(_require_admin)):
    """保存 SMTP 配置到 system_config，密码 AES 加密。密码字段留空则保持原值。"""
    from bottleneck_hunter.auth.crypto import encrypt
    store = _auth()
    if req.host is not None:
        store.set_config("smtp_host", req.host.strip())
    if req.port is not None:
        store.set_config("smtp_port", str(req.port))
    if req.user is not None:
        store.set_config("smtp_user", req.user.strip())
    if req.sender is not None:
        store.set_config("smtp_from", req.sender.strip())
    if req.use_tls is not None:
        store.set_config("smtp_use_tls", "true" if req.use_tls else "false")
    if req.password:  # 非空才更新；留空保持原密码
        store.set_config("smtp_password_enc", encrypt(req.password))
    return await get_smtp_config(user)


@router.post("/smtp-test")
async def test_smtp(req: SmtpTestRequest, user: dict = Depends(_require_admin)):
    """用当前生效的 SMTP 配置发送一封测试邮件。"""
    import asyncio
    from bottleneck_hunter.auth.email_sender import resolve_smtp_config, send_test_email, smtp_configured
    store = _auth()
    cfg = resolve_smtp_config(store)
    if not smtp_configured(cfg):
        raise HTTPException(status_code=400, detail="SMTP 未配置，请先填写并保存服务器地址")
    ok, err = await asyncio.to_thread(send_test_email, req.to_email, cfg)
    if not ok:
        raise HTTPException(status_code=502, detail=f"测试邮件发送失败：{err}")
    return {"ok": True, "message": f"测试邮件已发送至 {req.to_email}"}


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
