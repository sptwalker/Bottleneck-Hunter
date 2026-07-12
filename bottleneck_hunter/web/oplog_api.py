"""实时操作日志 API — 挂载于 /api/oplog。

- GET  /history   历史日志（近30天，可按 category 过滤 + before_ts 翻页）
- GET  /stream    实时 SSE（该用户新日志即时推送）
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.web.oplog import get_broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oplog"])

_store = None


def set_store(store) -> None:
    global _store
    _store = store


@router.get("/history")
async def oplog_history(category: str = "", before_ts: str = "", limit: int = 200,
                        user: dict = Depends(get_current_user)):
    """该用户的操作日志历史（时间倒序）。"""
    if _store is None:
        return {"logs": []}
    logs = _store.for_user(user["sub"]).get_operations(
        user["sub"], limit=limit, category=category, before_ts=before_ts)
    return {"logs": logs}


@router.get("/stream")
async def oplog_stream(request: Request, user: dict = Depends(get_current_user)):
    """该用户的实时操作日志 SSE。"""
    uid = user["sub"]
    q = get_broadcaster().subscribe(uid)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    rec = await asyncio.wait_for(q.get(), timeout=30)
                    yield {"event": "oplog", "data": json.dumps(rec, ensure_ascii=False)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            get_broadcaster().unsubscribe(uid, q)

    return EventSourceResponse(generate())
