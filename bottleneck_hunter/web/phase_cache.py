"""Phase 间结果缓存：服务端内存缓存 + TTL/LRU 淘汰。"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_TTL_SECONDS = 4 * 3600  # 4 小时
_MAX_ENTRIES = 50

_cache: dict[str, dict[str, Any]] = {}
_access_time: dict[str, float] = {}
_create_time: dict[str, float] = {}


def _touch(analysis_id: str) -> None:
    _access_time[analysis_id] = time.monotonic()


def _is_expired(analysis_id: str) -> bool:
    created = _create_time.get(analysis_id)
    if created is None:
        return False
    return (time.monotonic() - created) > _TTL_SECONDS


def _evict_if_needed() -> None:
    """超过 _MAX_ENTRIES 时淘汰最久未访问的条目。"""
    while len(_cache) >= _MAX_ENTRIES:
        oldest = min(_access_time, key=_access_time.get, default=None)
        if oldest is None:
            break
        logger.info("phase_cache LRU 淘汰: %s", oldest)
        _cache.pop(oldest, None)
        _access_time.pop(oldest, None)
        _create_time.pop(oldest, None)


def set_phase(analysis_id: str, phase: int, data: dict) -> None:
    """保存指定 phase 的结果，同时级联清除下游 phase。"""
    if analysis_id not in _cache:
        _evict_if_needed()
        _cache[analysis_id] = {}
        _create_time[analysis_id] = time.monotonic()
    _cache[analysis_id][str(phase)] = data
    for p in range(phase + 1, 5):
        _cache[analysis_id].pop(str(p), None)
    _touch(analysis_id)


def get_phase(analysis_id: str, phase: int) -> dict | None:
    """获取指定 phase 的缓存数据，过期自动淘汰。"""
    if _is_expired(analysis_id):
        clear(analysis_id)
        return None
    result = _cache.get(analysis_id, {}).get(str(phase))
    if result is not None:
        _touch(analysis_id)
    return result


def get_all(analysis_id: str) -> dict:
    """获取该 analysis_id 的所有 phase 数据。"""
    if _is_expired(analysis_id):
        clear(analysis_id)
        return {}
    _touch(analysis_id)
    return dict(_cache.get(analysis_id, {}))


def clear_from(analysis_id: str, phase: int) -> None:
    """清除 phase N 及其下游的全部缓存。"""
    entry = _cache.get(analysis_id)
    if not entry:
        return
    for p in range(phase, 5):
        entry.pop(str(p), None)


def clear(analysis_id: str) -> None:
    """清除整个 analysis_id 的缓存。"""
    _cache.pop(analysis_id, None)
    _access_time.pop(analysis_id, None)
    _create_time.pop(analysis_id, None)


def has_phase(analysis_id: str, phase: int) -> bool:
    if _is_expired(analysis_id):
        clear(analysis_id)
        return False
    return str(phase) in _cache.get(analysis_id, {})


def cache_stats() -> dict:
    """返回缓存统计信息（调试用）。"""
    return {
        "entries": len(_cache),
        "max_entries": _MAX_ENTRIES,
        "ttl_seconds": _TTL_SECONDS,
    }
