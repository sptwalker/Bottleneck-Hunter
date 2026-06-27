"""User API router — /api/user 端点（API KEY 管理等）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user"])

_auth_store = None


def set_auth_store(store):
    global _auth_store
    _auth_store = store


def _store():
    if _auth_store is None:
        raise HTTPException(status_code=500, detail="AuthStore not initialized")
    return _auth_store


# ── 请求模型 ──────────────────────────────────────────

class SaveApiKeyRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(..., min_length=1, max_length=512)


class TestApiKeyRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(default="", max_length=512)


# ── API KEY CRUD ─────────────────────────────────────

@router.get("/api-keys")
async def list_api_keys(user: dict = Depends(get_current_user)):
    """列出当前用户的所有 API KEY（只返回 hint 不返回明文）。"""
    store = _store()
    keys = store.get_user_api_keys(user["sub"])
    return {"keys": keys}


@router.post("/api-keys")
async def save_api_key(req: SaveApiKeyRequest, user: dict = Depends(get_current_user)):
    """保存或更新某 provider 的 API KEY（加密存储）。"""
    from bottleneck_hunter.auth.crypto import encrypt, make_hint
    from bottleneck_hunter.llm_clients.factory import PROVIDER_KEY_MAP

    provider = req.provider.lower().strip()
    if provider not in PROVIDER_KEY_MAP:
        raise HTTPException(status_code=400, detail=f"不支持的 provider: {provider}")

    encrypted = encrypt(req.api_key)
    hint = make_hint(req.api_key)

    store = _store()
    record_id = store.save_user_api_key(user["sub"], provider, encrypted, hint)
    logger.info(f"用户 {user.get('username', user['sub'])} 保存了 {provider} API KEY")
    return {"ok": True, "id": record_id, "provider": provider, "key_hint": hint}


@router.delete("/api-keys/{provider}")
async def delete_api_key(provider: str, user: dict = Depends(get_current_user)):
    """删除某 provider 的 API KEY。"""
    store = _store()
    removed = store.delete_user_api_key(user["sub"], provider.lower().strip())
    if not removed:
        raise HTTPException(status_code=404, detail="未找到该 provider 的 KEY")
    logger.info(f"用户 {user.get('username', user['sub'])} 删除了 {provider} API KEY")
    return {"ok": True}


@router.post("/api-keys/test")
async def test_api_key(req: TestApiKeyRequest, user: dict = Depends(get_current_user)):
    """测试某 provider 的 API KEY 是否可用。

    - 传入 api_key: 直接用该 KEY 测试（保存前预览）
    - 不传 api_key: 用已保存的 KEY 测试
    """
    import asyncio
    from langchain_core.messages import HumanMessage
    from bottleneck_hunter.llm_clients.factory import create_llm, PROVIDER_KEY_MAP

    provider = req.provider.lower().strip()
    if provider not in PROVIDER_KEY_MAP:
        raise HTTPException(status_code=400, detail=f"不支持的 provider: {provider}")

    # 确定要测试的 KEY
    test_key = req.api_key.strip() if req.api_key else None
    if not test_key:
        # 用已保存的 KEY
        test_key = _resolve_user_key(user["sub"], provider)

    if not test_key:
        raise HTTPException(status_code=400, detail=f"{provider} 未配置 API KEY")

    # 测试模型
    from bottleneck_hunter.web.api import DEFAULT_TEST_MODELS
    model = DEFAULT_TEST_MODELS.get(provider, "")
    if not model:
        raise HTTPException(status_code=400, detail=f"{provider} 无测试模型")

    try:
        llm = create_llm(provider, model, api_key=test_key)
        await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="hi")]),
            timeout=60,
        )
        return {"ok": True, "provider": provider, "model": model}
    except asyncio.TimeoutError:
        return {"ok": False, "provider": provider, "error": "请求超时（60s）"}
    except Exception as e:
        err_msg = str(e)
        if len(err_msg) > 120:
            err_msg = err_msg[:120] + "..."
        return {"ok": False, "provider": provider, "error": err_msg}


def _resolve_user_key(user_id: str, provider: str) -> str | None:
    """解密并返回用户的 API KEY（无则返回 None）。"""
    store = _store()
    encrypted = store.get_user_api_key_encrypted(user_id, provider)
    if not encrypted:
        return None
    try:
        from bottleneck_hunter.auth.crypto import decrypt
        return decrypt(encrypted)
    except Exception:
        return None


def resolve_user_api_key(user_id: str, provider: str) -> str | None:
    """公开方法：获取用户的解密 API KEY（供 factory 调用链使用）。"""
    return _resolve_user_key(user_id, provider)
