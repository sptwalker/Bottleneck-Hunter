"""Tests for the unified retry and timeout framework."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bottleneck_hunter.watchlist.retry import fetch_with_timeout, with_retry, get_http_client, close_http_client
import bottleneck_hunter.watchlist.retry as retry_mod


# ---------------------------------------------------------------------------
# @with_retry — sync
# ---------------------------------------------------------------------------

class TestWithRetrySync:
    def test_success_no_retry(self):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.01)
        def ok():
            nonlocal call_count
            call_count += 1
            return "done"

        assert ok() == "done"
        assert call_count == 1

    @patch("time.sleep")
    def test_retry_then_success(self, mock_sleep):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "recovered"

        assert flaky() == "recovered"
        assert call_count == 2
        assert mock_sleep.call_count == 1

    @patch("time.sleep")
    def test_retry_exhausted(self, mock_sleep):
        @with_retry(max_retries=3, base_delay=0.01)
        def always_fail():
            raise IOError("down")

        with pytest.raises(IOError, match="down"):
            always_fail()

    def test_no_retry_on_value_error(self):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.01)
        def bad_input():
            nonlocal call_count
            call_count += 1
            raise ValueError("bad")

        with pytest.raises(ValueError, match="bad"):
            bad_input()
        assert call_count == 1

    @patch("time.sleep")
    def test_exponential_backoff(self, mock_sleep):
        @with_retry(max_retries=3, base_delay=1.0)
        def fail_twice():
            raise TimeoutError("slow")

        with pytest.raises(TimeoutError):
            fail_twice()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0]


# ---------------------------------------------------------------------------
# @with_retry — async
# ---------------------------------------------------------------------------

class TestWithRetryAsync:
    async def test_async_success(self):
        @with_retry(max_retries=3, base_delay=0.01)
        async def ok():
            return "async_done"

        assert await ok() == "async_done"

    async def test_async_retry_then_success(self):
        call_count = 0

        @with_retry(max_retries=3, base_delay=0.001)
        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("fail")
            return "recovered"

        assert await flaky() == "recovered"
        assert call_count == 2


# ---------------------------------------------------------------------------
# fetch_with_timeout
# ---------------------------------------------------------------------------

class TestFetchWithTimeout:
    async def test_normal_completion(self):
        async def quick():
            return 42

        result = await fetch_with_timeout(quick(), timeout_sec=5)
        assert result == 42

    async def test_timeout_raises(self):
        async def slow():
            await asyncio.sleep(10)
            return "never"

        with pytest.raises(asyncio.TimeoutError):
            await fetch_with_timeout(slow(), timeout_sec=0.05)


# ---------------------------------------------------------------------------
# 共享 httpx 客户端
# ---------------------------------------------------------------------------

class TestHttpClient:
    async def test_get_http_client_reuses(self):
        """两次调用应返回同一实例。"""
        retry_mod._http_client = None
        c1 = get_http_client()
        c2 = get_http_client()
        assert c1 is c2
        await close_http_client()

    async def test_close_http_client(self):
        """关闭后下次获取应返回新实例。"""
        retry_mod._http_client = None
        c1 = get_http_client()
        await close_http_client()
        assert retry_mod._http_client is None
        c2 = get_http_client()
        assert c2 is not c1
        await close_http_client()
