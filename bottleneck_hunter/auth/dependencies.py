"""FastAPI 依赖注入：获取当前用户、要求管理员权限。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request


def get_current_user(request: Request) -> dict:
    """从 request.state 中取出 AuthMiddleware 注入的用户信息。

    返回 JWT payload dict，包含 sub, username, role 等字段。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """要求管理员权限。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_vip(user: dict = Depends(get_current_user)) -> dict:
    """要求 VIP 权限：admin 直通，或 users.settings_json.vip == true（回库读）。

    JWT payload 仅含 sub/username/role，vip 标记存 settings_json，故回库解析；
    后续可热点化进 JWT claim 免查库。
    """
    if user.get("role") == "admin":
        return user
    import json
    from bottleneck_hunter.auth.store import AuthStore
    try:
        u = AuthStore().get_user_by_id(user.get("sub", ""))
        settings = json.loads(getattr(u, "settings_json", "") or "{}") if u else {}
    except Exception:  # noqa: BLE001
        settings = {}
    if not settings.get("vip"):
        raise HTTPException(status_code=403, detail="需要 VIP 权限")
    return user
