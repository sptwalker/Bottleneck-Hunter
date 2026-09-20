"""取数总预算 —— 批量取数的「到点即停」，避免限流退避把整轮拖成小时级。

**为什么不是 `asyncio.wait_for` 了事**：批量取数跑在 `asyncio.to_thread` 的工作线程里，
`wait_for` 超时只能取消外层的 `await`，杀不掉已经在线程里阻塞的 `time.sleep` 退避，
也拿不回「已经取到的那部分」结果——一旦取消，`gather` 整体丢弃，等于白取。

**所以做协作式预算**：批量入口在**每个 ticker 取数前**查一次是否超时，超时就停止再发起、
把剩下的记为「未取（超预算）」，然后**正常返回已取到的部分**。这样：
- 结果不丢：已到手的财务/聪明钱数据照常进入评估；
- 不在途：新请求不再发出，Yahoo 那边不会继续被撞；
- 可解释：没取到的票进 `failed_tickers`，在结果页可见，不伪装成「数据就是空的」。

线程安全：`reset()` / `expired()` 都在事件循环线程里调用，读写的只是两个标量，无需加锁。
"""
from __future__ import annotations

import os
import time

# 一次 Phase 2 取数环节的总预算（秒）。默认 300s：正常一轮美股取数（数百家 × 3s 间隔的
# 全局节流）通常几十秒到两三分钟；给到 5 分钟是留足余量，同时把「限流退避无限拖」封顶。
_DEFAULT_BUDGET_SEC = float(os.environ.get("PHASE2_FETCH_BUDGET_SEC", "300"))

_started_at = 0.0
_budget_sec = _DEFAULT_BUDGET_SEC


def start(budget_sec: float | None = None) -> None:
    """标记本轮取数开始计时（Phase 2 取数环节入口调用）。"""
    global _started_at, _budget_sec
    _started_at = time.monotonic()
    _budget_sec = _DEFAULT_BUDGET_SEC if budget_sec is None else float(budget_sec)


def running() -> bool:
    """本轮预算是否仍在计时（已 `start()` 且尚未用尽）。

    给闸门用的「现在还有额度吗」：`elapsed()` 只回答「开始了多久」，预算用完的那一刻它依然
    在增长，闸门不能用它判断是否还该睡。未 `start()` 过返回 False（单测/CLI 不受约束）。
    """
    return _started_at > 0 and (time.monotonic() - _started_at) < _budget_sec


def expired() -> bool:
    """本轮预算是否已用尽。未 `start()` 过则视为未开始，永不过期（不误伤单测/其他调用方）。"""
    if _started_at <= 0:
        return False
    return (time.monotonic() - _started_at) >= _budget_sec


def remaining() -> float:
    """剩余预算秒数（未开始计时则返回预算原值）。"""
    if _started_at <= 0:
        return _budget_sec
    return max(0.0, _budget_sec - (time.monotonic() - _started_at))


def elapsed() -> float:
    """已用秒数（未开始计时返回 0）。"""
    return 0.0 if _started_at <= 0 else time.monotonic() - _started_at


def budget_sec() -> float:
    """本轮预算（秒）。"""
    return _budget_sec


def reset() -> None:
    """清空计时——下一轮取数开始前调用（与 `quotes_cache.reset()` 同处）。"""
    global _started_at
    _started_at = 0.0
