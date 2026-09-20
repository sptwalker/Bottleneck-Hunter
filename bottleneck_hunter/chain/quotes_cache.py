"""一次分析内的 Yahoo 取数缓存 —— 让财务链与聪明钱链共用同一份 `.info`，避免同票读两遍。

**为什么需要**：财务链（`financial_data._fetch_us_financial`）与聪明钱链（`smart_money._track_us_stock`）
都要 `yf.Ticker(t).info`，且两条链在 Phase 2 内并行跑同一批 ticker。此前各自独立取，
一只票的 `.info` 被打两遍——在 yf_gate 全局 3~30s 节流下，这直接等于凭空翻倍了等待时间。
2026-09 事故里 253 家候选 × 2 链 = 506 次 `.info`，其中一半是重复的。

**为什么是「按轮」而不是长 TTL**：`.info` 是财务/机构持仓类数据，同日重复用没有精度损失，
但如果做成进程级长缓存，用户明天再跑一次会拿到昨天的价格与持仓。故缓存生命周期严格等于
一次 Phase 2（`web/streaming/phases.py:stream_phase2` 开头 `reset()`），轮内共享、轮末即弃。

**线程安全**：取数都在 `asyncio.to_thread` 的工作线程里，缓存必须带锁。锁在「未命中 → 取数」
整个区间内持有：同一只票被两条链同时要时，第二个线程等第一个取完直接拿结果，
而不是两个线程各打一次 Yahoo。不同 ticker 之间由 yf_gate 全局节流天然排开，不额外损失并发。
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

_lock = threading.Lock()
_cache: dict[tuple[str, str], Any] = {}

# 取数失败的哨兵：区分「没查过」与「查过但失败」。失败不缓存（限流是暂时的，
# 下一次调用应当重试），故标记后立即丢弃。
_MISS = object()


def get(kind: str, ticker: str, fetch: Callable[[], Any]) -> Any:
    """取 `(kind, ticker)` 缓存；未命中则持锁调用 `fetch()` 并缓存非 None 结果。

    `fetch()` 抛异常时原样上抛且不缓存——调用方（financial_data / smart_money）已有
    `except Exception` 兜底并会 `observe(e)` 反馈给熔断器。
    """
    key = (kind, ticker)
    with _lock:
        hit = _cache.get(key, _MISS)
        if hit is not _MISS:
            return hit
        val = fetch()
        if val is not None:
            _cache[key] = val
        return val


def reset() -> None:
    """清空缓存——每次 Phase 2 开始前调用，保证轮内共享、轮间不串。"""
    with _lock:
        _cache.clear()


def stats() -> dict[str, int]:
    """缓存条目数（按 kind 分组）——供日志/诊断确认复用是否真的发生。"""
    with _lock:
        out: dict[str, int] = {}
        for kind, _ in _cache:
            out[kind] = out.get(kind, 0) + 1
        return out
