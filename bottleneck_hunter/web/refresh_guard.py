"""进程内每用户刷新互斥闸。

防止同一用户的多个刷新(观察池一键 / 日常决策 / 全量刷新)同时跑，
避免决策表互相覆盖、SQLite 写锁竞争、LLM 预算双倍消耗。

单进程 async 协作式调度：acquire 里 check+add 之间无 await，天然原子，无需真锁。
ponytail: 每用户单闸(不分市场)，最稳；若要美股/A股并发再把 key 细化到 user+market。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

_active: set[str] = set()

BUSY_MSG = "已有刷新任务在进行中，请等待其完成后再试"


def acquire(user_id: str) -> bool:
    """占闸成功返回 True；已有刷新在跑返回 False。"""
    if user_id in _active:
        return False
    _active.add(user_id)
    return True


def release(user_id: str) -> None:
    _active.discard(user_id)


async def guarded(user_id: str, gen: AsyncGenerator[dict, None]) -> AsyncGenerator[dict, None]:
    """包裹引擎生成器(data 为 dict)：忙则发一条 refresh_busy 事件并结束，否则占闸跑完释放。

    供 decision 侧 _sse_response 使用（它会把 data 再 json.dumps）。
    """
    if not acquire(user_id):
        yield {"event": "refresh_busy", "data": {"message": BUSY_MSG, "refresh_busy": True}}
        return
    try:
        async for evt in gen:
            yield evt
    finally:
        release(user_id)
