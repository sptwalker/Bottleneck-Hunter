"""付费数据源 Key 管理 API — 挂载于 /api/data-sources。

只做「配置 Key + 加密存储（按 user 隔离）+ 真实测连通」，不把数据接进分析链路。
复用 auth/crypto 的加密脱敏、auth/store 的 data_source_keys CRUD、data_source_catalog 的探测。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from bottleneck_hunter.auth.crypto import decrypt, encrypt, make_hint
from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.data_provider.data_source_catalog import (
    get_catalog,
    get_source_meta,
    probe_source,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data-sources"])

_auth_store = None


def set_auth_store(store):
    global _auth_store
    _auth_store = store


def _store():
    if _auth_store is None:
        raise HTTPException(status_code=500, detail="AuthStore not initialized")
    return _auth_store


class SaveKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=512)
    base_url: str = Field(default="", max_length=512)  # 自定义源的探测 URL（{KEY} 占位）


class TestRequest(BaseModel):
    source_id: str = Field(..., min_length=1, max_length=32)
    api_key: str = Field(default="", max_length=512)   # 空则回退已存的
    base_url: str = Field(default="", max_length=512)


@router.get("/catalog")
async def catalog(user: dict = Depends(get_current_user)):
    """预置数据源目录 + 该用户已配置状态（hint/base_url/configured）。"""
    uid = user.get("sub", "")
    configured = {c["source_id"]: c for c in _store().get_data_source_keys(uid)}
    out = []
    for src in get_catalog():
        cfg = configured.get(src["id"])
        out.append({
            **src,
            "configured": cfg is not None,
            "key_hint": cfg.get("key_hint", "") if cfg else "",
            "base_url_saved": cfg.get("base_url", "") if cfg else "",
        })
    return {"sources": out}


@router.post("/{source_id}/key")
async def save_key(source_id: str, req: SaveKeyRequest, user: dict = Depends(get_current_user)):
    if get_source_meta(source_id) is None:
        raise HTTPException(status_code=404, detail="未知数据源")
    uid = user.get("sub", "")
    _store().save_data_source_key(
        user_id=uid, source_id=source_id, base_url=req.base_url,
        encrypted_key=encrypt(req.api_key), key_hint=make_hint(req.api_key),
    )
    return {"ok": True, "source_id": source_id, "key_hint": make_hint(req.api_key)}


@router.delete("/{source_id}/key")
async def delete_key(source_id: str, user: dict = Depends(get_current_user)):
    ok = _store().delete_data_source_key(user.get("sub", ""), source_id)
    return {"ok": ok}


@router.post("/test")
async def test_source(req: TestRequest, user: dict = Depends(get_current_user)):
    """测连通。api_key 留空则用已存的解密 key；base_url 同理回退已存。未保存也能测。"""
    meta = get_source_meta(req.source_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="未知数据源")
    uid = user.get("sub", "")
    api_key = req.api_key
    base_url = req.base_url
    if not api_key:
        enc = _store().get_data_source_key_encrypted(uid, req.source_id)
        if enc:
            try:
                api_key = decrypt(enc)
            except Exception:  # noqa: BLE001
                api_key = ""
    if not base_url:
        base_url = _store().get_data_source_base_url(uid, req.source_id)
    ok, msg = await asyncio.to_thread(probe_source, req.source_id, api_key, base_url)
    return {"ok": ok, "source_id": req.source_id, "msg": msg}
