"""Tests for auto_execute.py — L4 自动执行三档开关与执行闭环。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from bottleneck_hunter.watchlist.auto_execute import (
    LEVEL_HIGH,
    LEVEL_OFF,
    LEVEL_SEMI,
    auto_execute_pending,
    get_auto_execute_level,
    is_auto_execute_enabled,
    is_breach_authorized,
    set_auto_execute,
    set_auto_execute_level,
)


class _KVStore:
    """最小 store 桩：仅实现偏好读写 + 待确认队列 + 投委会共识回读。"""

    def __init__(self, pending=None, verdict="approved"):
        self._kv = {}
        self._pending = pending or []
        # P0-A 独立判据：默认给「投委会背书」的共识，非背书场景由用例显式覆盖。
        self._verdict = verdict

    def get_preference(self, key, default=""):
        return self._kv.get(key, default)

    def save_preference(self, key, value, category=""):
        self._kv[key] = value

    def get_pending_executions(self):
        return self._pending

    def get_committee_consensus(self, _plan_id):
        if self._verdict is None:
            return None
        return {"final_verdict": self._verdict}


class TestSwitch:
    def test_default_off(self):
        assert is_auto_execute_enabled(_KVStore()) is False
        assert get_auto_execute_level(_KVStore()) == LEVEL_OFF
        assert is_breach_authorized(_KVStore()) is False

    def test_roundtrip(self):
        s = _KVStore()
        set_auto_execute(s, True)
        assert is_auto_execute_enabled(s) is True
        set_auto_execute(s, False)
        assert is_auto_execute_enabled(s) is False

    def test_bool_entry_maps_to_semi_not_high(self):
        """布尔入口（老前端）只能到半授权 —— 越线授权必须显式选高档，不能被 True 顺带打开。"""
        s = _KVStore()
        set_auto_execute(s, True)
        assert get_auto_execute_level(s) == LEVEL_SEMI
        assert is_breach_authorized(s) is False

    def test_three_levels(self):
        s = _KVStore()
        for lv in (LEVEL_OFF, LEVEL_SEMI, LEVEL_HIGH):
            set_auto_execute_level(s, lv)
            assert get_auto_execute_level(s) == lv
            assert is_auto_execute_enabled(s) is (lv >= LEVEL_SEMI)
            assert is_breach_authorized(s) is (lv >= LEVEL_HIGH)

    def test_legacy_values_read_back_correctly(self):
        """历史值无需迁移：""/"0"=关闭、"1"=半授权（旧开关的「已开」语义原样保留）。"""
        s = _KVStore()
        for raw, expected in (("", LEVEL_OFF), ("0", LEVEL_OFF), ("1", LEVEL_SEMI)):
            s.save_preference("auto_execute_l4", raw)
            assert get_auto_execute_level(s) == expected

    def test_dirty_and_out_of_range_degrade_to_off(self):
        """脏值/越界一律降级成最保守档：宁可多要一次人工确认，绝不静默提权。"""
        s = _KVStore()
        for raw in ("垃圾", "3", "-1", "1.5", None):
            s.save_preference("auto_execute_l4", raw)
            assert get_auto_execute_level(s) == LEVEL_OFF, raw
        set_auto_execute_level(s, 99)
        assert get_auto_execute_level(s) == LEVEL_HIGH  # 显式写入越界值则钳位到上限


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


class TestBreachGate:
    """越线闸门：半授权跳过越线、高授权才放开 —— 这是 P0-4 护栏的最后一道。"""

    _BREACH = {"id": "p1", "ticker": "NVDA", "result_json": {"mandate_exception": True}}
    _PLAIN = {"id": "p2", "ticker": "AAPL", "result_json": {"action": "buy"}}

    async def test_semi_authorized_skips_breach_plans(self):
        s = _KVStore(pending=[dict(self._BREACH), dict(self._PLAIN)])
        set_auto_execute_level(s, LEVEL_SEMI)
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 1                                  # 只成交非越线那条
        assert cae.await_args.args[1] == "p2"
        assert events[0]["data"]["exception_count"] == 1
        assert events[0]["data"]["level"] == LEVEL_SEMI

    async def test_semi_authorized_with_only_breach_executes_nothing(self):
        s = _KVStore(pending=[dict(self._BREACH)])
        set_auto_execute_level(s, LEVEL_SEMI)
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute",
                   AsyncMock(return_value={"status": "confirmed"})) as cae:
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 0
        assert [e["event"] for e in events] == ["auto_execute_skipped"]

    async def test_high_authorized_executes_breach_plans(self):
        s = _KVStore(pending=[dict(self._BREACH), dict(self._PLAIN)])
        set_auto_execute_level(s, LEVEL_HIGH)
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 2
        assert {c.args[1] for c in cae.await_args_list} == {"p1", "p2"}
        assert events[0]["data"]["exception_count"] == 0
        assert events[0]["data"]["level"] == LEVEL_HIGH
        assert next(e for e in events if e["event"] == "auto_execute_done")["data"]["executed"] == 2

    async def test_off_level_never_reaches_here(self):
        """关闭档下调用方根本不会进来；万一被调用，返回的就是原始队列（闸门只认档位）。"""
        s = _KVStore(pending=[dict(self._PLAIN)])
        set_auto_execute_level(s, LEVEL_OFF)
        assert is_auto_execute_enabled(s) is False      # 调用方的准入判据
        assert is_breach_authorized(s) is False


class TestCommitteeBackingGate:
    """P0-A（N-1）：status=pending 不等于「投委会通过了」——非结论性裁决必须拦住自动执行。

    needs_review（法定人数不足）/ needs_discussion（权重持平）/ unknown（结果缺失）此前都不落动作，
    计划以 pending 滞留，开关一开就成交 = 把「没人拍板」当成「默认放行」。
    """

    _P = {"id": "p1", "ticker": "AAPL"}

    @pytest.mark.parametrize("verdict", ["needs_review", "needs_discussion", "unknown"])
    async def test_non_decisive_verdicts_are_never_auto_executed(self, verdict):
        s = _KVStore(pending=[dict(self._P)], verdict=verdict)
        set_auto_execute_level(s, LEVEL_HIGH)  # 连高授权也不放行：背书缺失与授权档位无关
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 0
        assert [e["event"] for e in events] == ["auto_execute_skipped"]
        assert events[0]["data"]["reason"] == "no_committee_backing"
        assert events[0]["data"]["items"][0]["verdict"] == verdict

    async def test_modification_verdict_still_executes(self):
        """approve_with_modifications 是明确背书，照旧自动成交（不得收紧过头）。"""
        s = _KVStore(pending=[dict(self._P)], verdict="approved_with_modifications")
        set_auto_execute_level(s, LEVEL_SEMI)
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 1
        assert next(e for e in events if e["event"] == "auto_execute_done")["data"]["executed"] == 1

    async def test_missing_consensus_blocks(self):
        """共识行不存在（如 revert_to_pending 把结论丢了）→ 不背书，不成交。"""
        s = _KVStore(pending=[dict(self._P)], verdict=None)
        set_auto_execute_level(s, LEVEL_SEMI)
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 0
        assert events[0]["data"]["items"][0]["verdict"] == "no_consensus"

    async def test_consensus_read_failure_blocks_fail_closed(self):
        """读共识抛异常 → 按不背书处理（fail-closed），绝不因为读不到就放行。"""
        s = _KVStore(pending=[dict(self._P)])
        set_auto_execute_level(s, LEVEL_SEMI)
        s.get_committee_consensus = lambda _pid: (_ for _ in ()).throw(RuntimeError("db down"))
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 0
        assert events[0]["data"]["items"][0]["verdict"] == "consensus_read_error"

    async def test_mixed_queue_keeps_backed_and_skips_unbacked(self):
        """兜底覆盖：背书计划照成交，非背书计划拦住，互不干扰。"""
        s = _KVStore(pending=[{"id": "p1", "ticker": "AAPL"}, {"id": "p2", "ticker": "MSFT"}])
        set_auto_execute_level(s, LEVEL_SEMI)

        def _consensus(pid):
            return {"final_verdict": "approved" if pid == "p1" else "needs_review"}

        s.get_committee_consensus = _consensus
        cae = AsyncMock(return_value={"status": "confirmed"})
        with patch("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", cae):
            events = await _collect(auto_execute_pending(s, "us_stock"))
        assert cae.await_count == 1
        assert cae.await_args.args[1] == "p1"
        assert next(e for e in events if e["event"] == "auto_execute_done")["data"]["executed"] == 1
        assert any(e["event"] == "auto_execute_skipped" for e in events)
