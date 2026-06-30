"""认证相关 Pydantic 数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 请求 / 响应 模型
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32, pattern=r"^[a-zA-Z0-9_一-鿿]+$")
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=64)
    invite_code: str = Field(default="", max_length=32)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=6, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


# ---------------------------------------------------------------------------
# 用户模型
# ---------------------------------------------------------------------------

class UserInfo(BaseModel):
    """返回给前端的用户信息（不含敏感字段）。"""
    id: str
    username: str
    display_name: str = ""
    role: str = "user"
    is_active: bool = True
    watchlist_limit: int = 24
    created_at: Optional[str] = None
    last_login_at: Optional[str] = None


class UserInDB(UserInfo):
    """包含密码 hash 的完整用户记录。"""
    password_hash: str = ""
    settings_json: str = "{}"


# ---------------------------------------------------------------------------
# 邀请码
# ---------------------------------------------------------------------------

class InviteCode(BaseModel):
    code: str
    created_by: str = ""
    used_by: Optional[str] = None
    created_at: Optional[str] = None
    used_at: Optional[str] = None
    expires_at: Optional[str] = None
    is_active: bool = True
