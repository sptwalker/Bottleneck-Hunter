"""JWT 工具函数：创建 / 验证 token，设置 / 清除 cookie。"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Response

logger = logging.getLogger(__name__)

# JWT 密钥：优先从环境变量读取，否则首次运行自动生成
_JWT_SECRET: Optional[str] = None
_JWT_ALGORITHM = "HS256"
_JWT_EXPIRE_HOURS = 72  # token 有效期 3 天
_COOKIE_NAME = "bh_token"


def _get_secret() -> str:
    global _JWT_SECRET
    if _JWT_SECRET:
        return _JWT_SECRET
    _JWT_SECRET = os.getenv("BH_JWT_SECRET")
    if not _JWT_SECRET:
        secret_file = Path("data/.jwt_secret")
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            _JWT_SECRET = secret_file.read_text(encoding="utf-8").strip()
        else:
            _JWT_SECRET = secrets.token_hex(32)
            secret_file.write_text(_JWT_SECRET, encoding="utf-8")
            logger.info("已生成 JWT 密钥并保存到 data/.jwt_secret")
    return _JWT_SECRET


def create_token(user_id: str, username: str, role: str = "user") -> str:
    """创建 JWT token。"""
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=_JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _get_secret(), algorithm=_JWT_ALGORITHM)


def verify_token(token: str) -> Optional[dict]:
    """验证 JWT token。成功返回 payload dict，失败返回 None。"""
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=[_JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("JWT token 已过期")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"JWT token 无效: {e}")
        return None


def set_auth_cookie(response: Response, token: str, secure: bool = False):
    """在响应中设置 HttpOnly cookie。secure=True 时仅经 HTTPS 传输（生产由 X-Forwarded-Proto 判定）。"""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=_JWT_EXPIRE_HOURS * 3600,
        path="/",
    )


def clear_auth_cookie(response: Response):
    """清除认证 cookie。"""
    response.delete_cookie(key=_COOKIE_NAME, path="/")


def get_cookie_name() -> str:
    return _COOKIE_NAME
