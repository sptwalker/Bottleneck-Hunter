"""自定义 OpenAI 兼容 Provider 管理 API。"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.llm_clients.factory import (
    PROVIDER_KEY_MAP,
    create_llm,
    get_custom_provider,
    register_custom_provider,
    unregister_custom_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["custom-providers"])

_auth_store = None


def set_auth_store(store):
    global _auth_store
    _auth_store = store


def _store():
    if _auth_store is None:
        raise HTTPException(status_code=500, detail="AuthStore not initialized")
    return _auth_store


_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


class CustomProviderRequest(BaseModel):
    provider_id: str = Field(..., min_length=2, max_length=32)
    display_name: str = Field(..., min_length=1, max_length=64)
    base_url: str = Field(default="", max_length=512)  # 允许空：openai/anthropic/google 走 SDK 默认端点
    api_key: str = Field(default="", max_length=512)
    default_model: str = Field(..., min_length=1, max_length=128)


@router.get("")
async def list_providers(user: dict = Depends(get_current_user)):
    """列出所有 provider（统一真源 custom_providers）。"""
    store = _store()
    providers = store.list_custom_providers()
    return {"providers": providers}


@router.post("")
async def create_provider(req: CustomProviderRequest, user: dict = Depends(get_current_user)):
    """添加 provider（OpenAI 兼容或原内置同构）。"""
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    pid = req.provider_id.lower().strip()
    if not _PROVIDER_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="provider_id 格式无效（小写字母开头，仅含字母/数字/下划线/连字符）")

    encrypted = encrypt(req.api_key) if req.api_key else ""
    hint = make_hint(req.api_key) if req.api_key else ""

    store = _store()
    record_id = store.save_custom_provider(
        pid, req.display_name, req.base_url, encrypted, hint, req.default_model,
    )

    api_key_plain = req.api_key if req.api_key else ""
    register_custom_provider(pid, req.base_url, api_key_plain, req.default_model)

    logger.info("自定义 provider 已创建: %s (%s)", pid, req.base_url)
    return {"ok": True, "id": record_id, "provider_id": pid}


@router.put("/{provider_id}")
async def update_provider(provider_id: str, req: CustomProviderRequest, user: dict = Depends(get_current_user)):
    """更新自定义 provider。"""
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    store = _store()
    existing = store.get_custom_provider(provider_id)
    if not existing:
        raise HTTPException(status_code=404, detail="未找到该自定义 provider")

    encrypted = existing.get("api_key_encrypted", "")
    hint = existing.get("api_key_hint", "")
    api_key_plain = ""

    if req.api_key:
        encrypted = encrypt(req.api_key)
        hint = make_hint(req.api_key)
        api_key_plain = req.api_key
    elif encrypted:
        from bottleneck_hunter.auth.crypto import decrypt
        try:
            api_key_plain = decrypt(encrypted)
        except Exception:
            api_key_plain = ""

    store.save_custom_provider(
        provider_id, req.display_name, req.base_url, encrypted, hint, req.default_model,
    )

    register_custom_provider(provider_id, req.base_url, api_key_plain, req.default_model)

    logger.info("自定义 provider 已更新: %s", provider_id)
    return {"ok": True, "provider_id": provider_id}


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str, user: dict = Depends(get_current_user)):
    """删除 provider（统一真源）。

    若该 id 是原内置 provider（在 PROVIDER_KEY_MAP 中），同步清除其 .env/os.environ 全局 Key、
    该用户加密 Key 与 provider_configs 覆盖，保证删干净——重启后不会被迁移逻辑复活。
    """
    store = _store()
    removed = store.delete_custom_provider(provider_id)
    if not removed:
        raise HTTPException(status_code=404, detail="未找到该 provider")

    unregister_custom_provider(provider_id)

    # 若为原内置 provider，彻底清理遗留全局/用户级 Key 与覆盖配置（否则启动迁移会重新建卡片）
    if provider_id in PROVIDER_KEY_MAP:
        _purge_builtin_residue(provider_id, user.get("sub", ""))

    logger.info("provider 已删除: %s", provider_id)
    return {"ok": True}


def _purge_builtin_residue(provider_id: str, user_id: str) -> None:
    """清除原内置 provider 的 .env/os.environ Key、用户加密 Key、provider_configs 覆盖。"""
    import os
    from pathlib import Path

    from dotenv import set_key

    env_var = PROVIDER_KEY_MAP.get(provider_id, "")
    # 1) .env + os.environ 全局 Key
    if env_var:
        try:
            env_path = Path.cwd() / ".env"
            if env_path.exists():
                set_key(str(env_path), env_var, "")
        except Exception as e:
            logger.warning("清除 .env Key 失败 (%s): %s", env_var, e)
        os.environ.pop(env_var, None)
    # 2) 用户加密 Key
    if user_id:
        try:
            _store().delete_user_api_key(user_id, provider_id)
        except Exception as e:
            logger.debug("清除用户 Key 失败 (%s): %s", provider_id, e)
    # 3) provider_configs 覆盖（全局 + 用户级）
    try:
        from bottleneck_hunter.watchlist.store import WatchlistStore
        wl = WatchlistStore()
        wl.delete_provider_config(provider_id, user_id="")
        if user_id:
            wl.delete_provider_config(provider_id, user_id=user_id)
    except Exception as e:
        logger.debug("清除 provider_configs 覆盖失败 (%s): %s", provider_id, e)


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, user: dict = Depends(get_current_user)):
    """测试自定义 provider 连通性。"""
    custom = get_custom_provider(provider_id)
    if not custom:
        raise HTTPException(status_code=404, detail="未找到该自定义 provider")

    try:
        llm = create_llm(
            provider_id,
            custom["default_model"],
            api_key=custom.get("api_key"),
        )
        await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="hi")]),
            timeout=30,
        )
        return {"ok": True, "provider_id": provider_id, "model": custom["default_model"]}
    except asyncio.TimeoutError:
        return {"ok": False, "provider_id": provider_id, "error": "请求超时（30s）"}
    except Exception as e:
        err_msg = str(e)
        if len(err_msg) > 120:
            err_msg = err_msg[:120] + "..."
        return {"ok": False, "provider_id": provider_id, "error": err_msg}
