"""phase_cache 单元测试。

覆盖 set_phase / get_phase / get_all / clear_from / clear / has_phase / cache_stats
以及 TTL 过期淘汰和 LRU 满载淘汰逻辑。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bottleneck_hunter.web import phase_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    """每个测试前后清空缓存全局状态。"""
    phase_cache._cache.clear()
    phase_cache._access_time.clear()
    phase_cache._create_time.clear()
    yield
    phase_cache._cache.clear()
    phase_cache._access_time.clear()
    phase_cache._create_time.clear()


# ---------------------------------------------------------------------------
# TestSetPhase
# ---------------------------------------------------------------------------


class TestSetPhase:
    """测试 set_phase 写入缓存的各种场景。"""

    def test_set_creates_entry(self):
        phase_cache.set_phase("a1", 1, {"result": "ok"})
        assert "a1" in phase_cache._cache
        assert phase_cache._cache["a1"]["1"] == {"result": "ok"}

    def test_set_multiple_phases(self):
        phase_cache.set_phase("a1", 1, {"p1": True})
        phase_cache.set_phase("a1", 2, {"p2": True})
        assert "1" in phase_cache._cache["a1"]
        assert "2" in phase_cache._cache["a1"]

    def test_set_invalidates_downstream(self):
        """写入 phase 2 后，phase 3 和 4 应被清除。"""
        phase_cache.set_phase("a1", 1, {"p1": True})
        phase_cache.set_phase("a1", 2, {"p2": True})
        phase_cache.set_phase("a1", 3, {"p3": True})
        phase_cache.set_phase("a1", 4, {"p4": True})
        # 重新写 phase 2 → phase 3, 4 被清
        phase_cache.set_phase("a1", 2, {"p2": "updated"})
        assert "1" in phase_cache._cache["a1"]
        assert phase_cache._cache["a1"]["2"] == {"p2": "updated"}
        assert "3" not in phase_cache._cache["a1"]
        assert "4" not in phase_cache._cache["a1"]

    def test_set_updates_access_and_create_time(self):
        phase_cache.set_phase("a1", 1, {"x": 1})
        assert "a1" in phase_cache._access_time
        assert "a1" in phase_cache._create_time


# ---------------------------------------------------------------------------
# TestGetPhase
# ---------------------------------------------------------------------------


class TestGetPhase:
    """测试 get_phase 读取与过期逻辑。"""

    def test_get_existing(self):
        phase_cache.set_phase("a1", 1, {"val": 42})
        assert phase_cache.get_phase("a1", 1) == {"val": 42}

    def test_get_nonexistent_analysis(self):
        assert phase_cache.get_phase("no_such", 1) is None

    def test_get_nonexistent_phase(self):
        phase_cache.set_phase("a1", 1, {"x": 1})
        assert phase_cache.get_phase("a1", 3) is None

    def test_get_expired_returns_none(self):
        """TTL 过期后 get_phase 返回 None 并清除缓存。"""
        phase_cache.set_phase("a1", 1, {"x": 1})
        real_create = phase_cache._create_time["a1"]
        # 让 monotonic 返回一个足够大的值使 TTL 过期
        with patch("bottleneck_hunter.web.phase_cache.time") as mock_time:
            mock_time.monotonic.return_value = real_create + phase_cache._TTL_SECONDS + 1
            result = phase_cache.get_phase("a1", 1)
        assert result is None
        assert "a1" not in phase_cache._cache


# ---------------------------------------------------------------------------
# TestGetAll
# ---------------------------------------------------------------------------


class TestGetAll:
    """测试 get_all 返回全部 phase 数据。"""

    def test_get_all_returns_copy(self):
        phase_cache.set_phase("a1", 1, {"p1": True})
        phase_cache.set_phase("a1", 2, {"p2": True})
        result = phase_cache.get_all("a1")
        assert result == {"1": {"p1": True}, "2": {"p2": True}}
        # 返回值是副本
        result["999"] = "injected"
        assert "999" not in phase_cache._cache["a1"]

    def test_get_all_nonexistent(self):
        assert phase_cache.get_all("no_such") == {}


# ---------------------------------------------------------------------------
# TestClearFrom
# ---------------------------------------------------------------------------


class TestClearFrom:
    """测试 clear_from 清除指定 phase 及下游。"""

    def test_clear_from_removes_downstream(self):
        for p in range(1, 5):
            phase_cache.set_phase("a1", p, {f"p{p}": True})
        phase_cache.clear_from("a1", 2)
        assert "1" in phase_cache._cache["a1"]
        assert "2" not in phase_cache._cache["a1"]
        assert "3" not in phase_cache._cache["a1"]
        assert "4" not in phase_cache._cache["a1"]

    def test_clear_from_nonexistent(self):
        """对不存在的 analysis_id 调用不报错。"""
        phase_cache.clear_from("no_such", 1)


# ---------------------------------------------------------------------------
# TestClear
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_removes_all(self):
        phase_cache.set_phase("a1", 1, {"x": 1})
        phase_cache.clear("a1")
        assert "a1" not in phase_cache._cache
        assert "a1" not in phase_cache._access_time
        assert "a1" not in phase_cache._create_time


# ---------------------------------------------------------------------------
# TestHasPhase
# ---------------------------------------------------------------------------


class TestHasPhase:
    def test_has_existing(self):
        phase_cache.set_phase("a1", 2, {"x": 1})
        assert phase_cache.has_phase("a1", 2) is True

    def test_has_nonexistent(self):
        assert phase_cache.has_phase("no_such", 1) is False

    def test_has_expired(self):
        phase_cache.set_phase("a1", 1, {"x": 1})
        real_create = phase_cache._create_time["a1"]
        with patch("bottleneck_hunter.web.phase_cache.time") as mock_time:
            mock_time.monotonic.return_value = real_create + phase_cache._TTL_SECONDS + 1
            assert phase_cache.has_phase("a1", 1) is False


# ---------------------------------------------------------------------------
# TestCacheStats
# ---------------------------------------------------------------------------


class TestCacheStats:
    def test_stats_format(self):
        phase_cache.set_phase("a1", 1, {"x": 1})
        phase_cache.set_phase("a2", 1, {"x": 2})
        stats = phase_cache.cache_stats()
        assert stats["entries"] == 2
        assert stats["max_entries"] == phase_cache._MAX_ENTRIES
        assert stats["ttl_seconds"] == phase_cache._TTL_SECONDS


# ---------------------------------------------------------------------------
# TestEviction — LRU 满载淘汰
# ---------------------------------------------------------------------------


class TestEviction:
    def test_lru_eviction(self):
        """缓存满载时，最久未访问的条目被淘汰。"""
        tick = 100.0

        def next_tick():
            nonlocal tick
            tick += 1.0
            return tick

        with patch.object(phase_cache, "_MAX_ENTRIES", 3), \
             patch("bottleneck_hunter.web.phase_cache.time") as mock_time:
            mock_time.monotonic.side_effect = next_tick
            phase_cache.set_phase("a1", 1, {"x": 1})  # access=101,102
            phase_cache.set_phase("a2", 1, {"x": 2})  # access=103,104
            phase_cache.set_phase("a3", 1, {"x": 3})  # access=105,106
            phase_cache.get_phase("a1", 1)             # access[a1]=108 (107 for _is_expired)
            phase_cache.set_phase("a4", 1, {"x": 4})  # evicts a2(104), access[a4]=110
            assert "a1" in phase_cache._cache
            assert "a2" not in phase_cache._cache
            assert "a3" in phase_cache._cache
            assert "a4" in phase_cache._cache

    def test_eviction_removes_all_metadata(self):
        """淘汰时 access_time / create_time 也被清除。"""
        with patch.object(phase_cache, "_MAX_ENTRIES", 2):
            phase_cache.set_phase("a1", 1, {"x": 1})
            phase_cache.set_phase("a2", 1, {"x": 2})
            phase_cache.set_phase("a3", 1, {"x": 3})
            assert "a1" not in phase_cache._access_time
            assert "a1" not in phase_cache._create_time
