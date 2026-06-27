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
