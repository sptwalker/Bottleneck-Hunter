"""自定义 OpenAI 兼容 Provider 管理 API。"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user, require_admin
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
async def create_provider(req: CustomProviderRequest, user: dict = Depends(require_admin)):
    """添加 provider（OpenAI 兼容或原内置同构）。仅管理员——provider 定义（base_url/模型）全平台共享。

    严格隔离：API Key **不再**全局存储，而是写入创建者自己的用户级加密表；其他用户
    需各自在配置中心为该 provider 填入自己的 Key 才能使用。
    """
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    pid = req.provider_id.lower().strip()
    if not _PROVIDER_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="provider_id 格式无效（小写字母开头，仅含字母/数字/下划线/连字符）")

    store = _store()
    # provider 定义共享，但不含密钥（encrypted/hint 恒为空）
    record_id = store.save_custom_provider(
        pid, req.display_name, req.base_url, "", "", req.default_model,
    )
    # 密钥写入创建者的用户级存储
    uid = user.get("sub", "")
    if req.api_key and uid:
        store.save_user_api_key(uid, pid, encrypt(req.api_key), make_hint(req.api_key))

    register_custom_provider(pid, req.base_url, default_model=req.default_model)

    logger.info("自定义 provider 已创建: %s (%s)", pid, req.base_url)
    return {"ok": True, "id": record_id, "provider_id": pid}


@router.put("/{provider_id}")
async def update_provider(provider_id: str, req: CustomProviderRequest, user: dict = Depends(require_admin)):
    """更新自定义 provider。仅管理员——base_url 可覆盖，普通用户改动会劫持全平台 LLM 流量。

    严格隔离：若传了新 api_key，仅更新到当前管理员自己的用户级存储，不落全局。
    """
    from bottleneck_hunter.auth.crypto import encrypt, make_hint

    store = _store()
    existing = store.get_custom_provider(provider_id)
    if not existing:
        raise HTTPException(status_code=404, detail="未找到该自定义 provider")

    # provider 定义共享，密钥不落全局
    store.save_custom_provider(
        provider_id, req.display_name, req.base_url, "", "", req.default_model,
    )
    uid = user.get("sub", "")
    if req.api_key and uid:
        store.save_user_api_key(uid, provider_id, encrypt(req.api_key), make_hint(req.api_key))

    register_custom_provider(provider_id, req.base_url, default_model=req.default_model)

    logger.info("自定义 provider 已更新: %s", provider_id)
    return {"ok": True, "provider_id": provider_id}


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str, user: dict = Depends(require_admin)):
    """删除 provider（统一真源）。仅管理员——删除影响全平台。

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
        # 用当前用户自己的 Key 测试（严格隔离，不用全局）
        from bottleneck_hunter.web.user_api import resolve_user_api_key
        uid = user.get("sub", "")
        user_key = resolve_user_api_key(uid, provider_id) if uid else None
        if not user_key:
            return {"ok": False, "provider_id": provider_id, "error": "你尚未为该 provider 配置 API Key"}
        llm = create_llm(
            provider_id,
            custom["default_model"],
            api_key=user_key,
            with_fallback=False,
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
