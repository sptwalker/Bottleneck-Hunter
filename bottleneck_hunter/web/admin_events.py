"""Admin 实时通知广播 — 内存 pub/sub，供 admin 面板右侧通知区消费。

照 syslog_api.LogBroadcaster 的形状精简（去掉 logging handler，只留 pub/sub）：
广播「用户登录 / 开始产业链分析 / 全量更新」等管理员关心的事件，SSE 端点见 admin_api。
无订阅者时 broadcast 仅入历史环、不报错；新订阅者连上时回放最近 MAX_HISTORY 条。
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime, timezone

MAX_QUEUE_SIZE = 200
MAX_HISTORY = 30

_id_counter = itertools.count(1)


class AdminEventBroadcaster:
    """把管理员事件广播给所有已连接的 admin SSE 客户端。"""

    def __init__(self):
        self._queues: set[asyncio.Queue] = set()
        self._history: list[dict] = []

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


_broadcaster = AdminEventBroadcaster()


def get_admin_broadcaster() -> AdminEventBroadcaster:
    return _broadcaster


def notify_admins(kind: str, title: str, username: str = "", detail: str = "") -> None:
    """埋点调用入口：组事件记录并广播。kind ∈ {login, analysis, full_refresh, ...}。

    非阻塞、异常不外泄——埋点处不应因通知失败而受影响。
    """
    try:
        rec = {
            "id": next(_id_counter),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "title": title,
            "username": username,
            "detail": detail,
        }
        _broadcaster.broadcast(rec)
    except Exception:  # noqa: BLE001 通知失败绝不影响业务
        pass
