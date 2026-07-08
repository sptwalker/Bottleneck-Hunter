"""SSE 提示投递：把 fallback.drain_notices() 收集到的「模型已替换」提示
穿插进任意事件流，作为 `model_fallback` 事件发给前端。

两种 SSE 载荷约定：
- pipeline 流（web/api.py / reverse 等）用 streaming/_common._sse，data 预序列化成 JSON 字符串；
- watchlist/decision 流用 dict 版 _sse，由 decision_api._sse_response 在边界序列化。
`with_notices` 接收对应的 sse_fn 以匹配 host 约定。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from bottleneck_hunter.llm_clients.fallback import begin_notices, drain_notices


async def with_notices(gen: AsyncGenerator, sse_fn) -> AsyncGenerator:
    """包装事件流：开启提示收集，并在每个事件后 flush 出 model_fallback 事件。

    sse_fn: 形如 _sse(event, **data) 的函数，返回该 host 约定的事件 dict。
    """
    begin_notices()
    try:
        async for evt in gen:
            yield evt
            for n in drain_notices():
                yield sse_fn("model_fallback", **n)
    finally:
        for n in drain_notices():
            yield sse_fn("model_fallback", **n)
