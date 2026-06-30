"""系统日志实时推送 API — 挂载于 /api/system

通过 SSE 将 Python logging 日志实时推送到前端底部信息栏。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.auth.dependencies import get_current_user

router = APIRouter(tags=["system"])

MAX_QUEUE_SIZE = 200
MAX_HISTORY = 30


class LogBroadcaster:
    """将 Python 日志广播给所有 SSE 客户端。"""

    def __init__(self):
        self._queues: set[asyncio.Queue] = set()
        self._handler: _BroadcastHandler | None = None
        self._history: list[dict] = []

    def install(self, level: int = logging.INFO):
        self._handler = _BroadcastHandler(self)
        self._handler.setLevel(level)
        self._handler.setFormatter(logging.Formatter("%(name)s | %(message)s"))
        logging.getLogger().addHandler(self._handler)

    def uninstall(self):
        if self._handler:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

    def broadcast(self, record: dict):
        self._history.append(record)
        if len(self._history) > MAX_HISTORY:
            self._history = self._history[-MAX_HISTORY:]
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.discard(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        for rec in self._history:
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                break
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._queues.discard(q)


class _BroadcastHandler(logging.Handler):
    def __init__(self, broadcaster: LogBroadcaster):
        super().__init__()
        self._broadcaster = broadcaster
        self._ignore = {"uvicorn.access", "uvicorn.error", "watchfiles", "apscheduler.scheduler", "apscheduler.executors"}

    def emit(self, record: logging.LogRecord):
        if record.name in self._ignore:
            return
        for prefix in self._ignore:
            if record.name.startswith(prefix + "."):
                return
        try:
            msg = self.format(record)
            self._broadcaster.broadcast({
                "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "level": record.levelname.lower(),
                "msg": msg,
            })
        except Exception:
            pass


_broadcaster: LogBroadcaster | None = None


def init_broadcaster() -> LogBroadcaster:
    global _broadcaster
    _broadcaster = LogBroadcaster()
    _broadcaster.install()
    return _broadcaster


def shutdown_broadcaster():
    global _broadcaster
    if _broadcaster:
        _broadcaster.uninstall()
        _broadcaster = None


@router.get("/logs")
async def stream_logs(request: Request, user: dict = Depends(get_current_user)):
    if not _broadcaster:
        return {"error": "日志广播未初始化"}

    q = _broadcaster.subscribe()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    record = await asyncio.wait_for(q.get(), timeout=30)
                    yield {
                        "event": "log",
                        "data": json.dumps(record, ensure_ascii=False),
                    }
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        finally:
            _broadcaster.unsubscribe(q)

    return EventSourceResponse(generate())
