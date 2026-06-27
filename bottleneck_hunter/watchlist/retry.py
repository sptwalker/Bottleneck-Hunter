"""统一重试与超时框架 — 为数据管道提供 @with_retry 装饰器和 fetch_with_timeout。"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging

logger = logging.getLogger(__name__)

RETRYABLE_EXCEPTIONS = (IOError, TimeoutError, ConnectionError, OSError)


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    retry_on: tuple[type[Exception], ...] = RETRYABLE_EXCEPTIONS,
):
    """装饰器：对函数的可重试异常做指数退避重试。

    支持同步和异步函数。重试耗尽后抛出最后一次异常。
    不在白名单内的异常立即抛出。
    """

    def decorator(func):
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                last_exc = None
                for attempt in range(max_retries):
                    try:
                        return await func(*args, **kwargs)
                    except retry_on as e:
                        last_exc = e
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                "%s 第 %d/%d 次重试（%.1fs 后）: %s",
                                func.__name__, attempt + 1, max_retries, delay, e,
                            )
                            await asyncio.sleep(delay)
                raise last_exc

            return async_wrapper
        else:
            import time

            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                last_exc = None
                for attempt in range(max_retries):
                    try:
                        return func(*args, **kwargs)
                    except retry_on as e:
                        last_exc = e
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.warning(
                                "%s 第 %d/%d 次重试（%.1fs 后）: %s",
                                func.__name__, attempt + 1, max_retries, delay, e,
                            )
                            time.sleep(delay)
                raise last_exc

            return sync_wrapper

    return decorator


async def fetch_with_timeout(coro, timeout_sec: float = 20):
    """统一超时包装。超时后抛 asyncio.TimeoutError（可被 @with_retry 捕获重试）。"""
    return await asyncio.wait_for(coro, timeout=timeout_sec)


# ---------------------------------------------------------------------------
# 共享 httpx 客户端
# ---------------------------------------------------------------------------

import httpx

_http_client: httpx.AsyncClient | None = None


def get_http_client(timeout: float = 20) -> httpx.AsyncClient:
    """获取共享的 httpx 异步客户端（延迟创建，跨请求复用连接池）。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=timeout)
    return _http_client


async def close_http_client():
    """关闭共享客户端。应用关闭时调用。"""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None
