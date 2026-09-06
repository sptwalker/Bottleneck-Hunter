"""Tests for auto_execute.py — L4 自动执行开关与执行闭环。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bottleneck_hunter.watchlist.auto_execute import (
    auto_execute_pending,
    is_auto_execute_enabled,
    set_auto_execute,
)


class _KVStore:
    """最小 store 桩：仅实现偏好读写 + 待确认队列。"""

    def __init__(self, pending=None):
        self._kv = {}
        self._pending = pending or []

    def get_preference(self, key, default=""):
        return self._kv.get(key, default)

    def save_preference(self, key, value, category=""):
        self._kv[key] = value

    def get_pending_executions(self):
        return self._pending


class TestSwitch:
    def test_default_off(self):
        assert is_auto_execute_enabled(_KVStore()) is False

    def test_roundtrip(self):
        s = _KVStore()
        set_auto_execute(s, True)
        assert is_auto_execute_enabled(s) is True
        set_auto_execute(s, False)
        assert is_auto_execute_enabled(s) is False


async def _collect(agen):
    return [e async for e in agen]


class TestAutoExecutePending:
    async def test_no_pending_yields_nothing(self):
        events = await _collect(auto_execute_pending(_KVStore(pending=[]), "us_stock"))
        assert events == []

    async def test_all_confirmed_counts(self):
        store = _KVStore(pending=[{"id": "p1", "ticker": "AAPL"}, {"id": "p2", "ticker": "MSFT"}])
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(store, "us_stock"))
        assert cae.await_count == 2
        done = next(e for e in events if e["event"] == "auto_execute_done")
        assert done["data"]["executed"] == 2
        assert done["data"]["failed"] == 0

    async def test_single_failure_does_not_abort_rest(self):
        # 首条抛异常，第二条 resting，第三条 confirmed —— 全部被处理，计数正确
        store = _KVStore(pending=[
            {"id": "p1", "ticker": "A"}, {"id": "p2", "ticker": "B"}, {"id": "p3", "ticker": "C"},
        ])
        results = [RuntimeError("boom"), {"status": "resting"}, {"status": "confirmed"}]

        async def _side(_store, _plan_id):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", side_effect=_side):
            events = await _collect(auto_execute_pending(store, "us_stock"))
        done = next(e for e in events if e["event"] == "auto_execute_done")
        assert done["data"]["executed"] == 1
        assert done["data"]["rested"] == 1
        assert done["data"]["failed"] == 1

    async def test_business_error_counts_as_failed(self):
        store = _KVStore(pending=[{"id": "p1", "ticker": "AAPL"}])
        cae = AsyncMock(return_value={"status": "error", "message": "现金不足"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(store, "us_stock"))
        done = next(e for e in events if e["event"] == "auto_execute_done")
        assert done["data"]["failed"] == 1
        assert done["data"]["executed"] == 0
