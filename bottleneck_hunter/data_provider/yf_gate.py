"""全局 Yahoo/yfinance 调用节流闸门 —— 把「一键刷新」的突发轰炸摊成均匀细流，避免 429/熔断。

**为什么是限速不是限并发**：Semaphore 只限「同时几个」，Yahoo 是按「多快打一次」限流的，
必须卡最小间隔。机构/分析师/新闻/期权/价格五条链此前只有 per-pipeline 的 Semaphore、零限速，
一只票 429 了立刻打下一只，持续硬撞——这是全表限流的根因（详见 institutional_pipeline 等）。

**为什么做成同步**：所有直连 yfinance 的取数都是同步函数、跑在 `asyncio.to_thread` 的线程池里。
同步闸门可以在每个取数函数开头一行 `throttle()` 接入，无需改动每层 async 包装。`time.sleep`
发生在工作线程里，不阻塞事件循环。全局单闸，五条链共享一条节流，互不越权。

**自适应**：`observe(err)` 反馈每次调用结果——撞到 429/限流就把间隔翻倍退避（上限 YF_MAX_INTERVAL），
连续成功再按 0.9 缓降回 YF_MIN_INTERVAL。这样限流一冒头就自动降速，风头过了自动提速。

调参（环境变量，均为秒）：YF_MIN_INTERVAL 默认 1.5、YF_MAX_INTERVAL 默认 30。
"""
from __future__ import annotations

import os
import threading
import time

_MIN = float(os.environ.get("YF_MIN_INTERVAL", "1.5"))
_MAX = float(os.environ.get("YF_MAX_INTERVAL", "30"))

_lock = threading.Lock()
_next_at = 0.0          # 下一次允许发起调用的最早时刻（monotonic 时钟）
_interval = _MIN        # 当前自适应间隔


def throttle() -> None:
    """在真正打 Yahoo 前调用：领一个均匀间隔的时间槽并睡到该槽。全局串行错峰。"""
    global _next_at
    with _lock:
        now = time.monotonic()
        start = now if now >= _next_at else _next_at
        _next_at = start + _interval
        wait = start - now
    if wait > 0:
        time.sleep(wait)   # 在 to_thread 的工作线程里睡，不碰事件循环


def observe(err: object | None = None) -> None:
    """调用结果反馈：err 命中 429/限流 → 间隔翻倍退避；否则缓降回下限。"""
    global _interval
    with _lock:
        if err is not None and _is_rate_limit(err):
            _interval = min(_MAX, max(_interval, _MIN) * 2.0)
        else:
            _interval = max(_MIN, _interval * 0.9)


def _is_rate_limit(err: object) -> bool:
    s = str(err).lower()
    return "too many requests" in s or "rate limited" in s or "429" in s


def current_interval() -> float:
    """当前自适应间隔（秒）——供健康面板/诊断读取。"""
    return _interval


def _reset() -> None:
    """仅供测试：清回初始状态。"""
    global _next_at, _interval
    with _lock:
        _next_at = 0.0
        _interval = _MIN


if __name__ == "__main__":
    # ponytail: 自检——节流确实错峰、429 退避会翻倍、成功会缓降
    _reset()
    throttle()                      # 首次不等待
    t0 = time.monotonic()
    throttle()                      # 第二次须等 ~_MIN
    dt = time.monotonic() - t0
    assert dt >= _MIN * 0.95, f"节流未生效: 间隔仅 {dt:.3f}s < {_MIN}s"

    base = current_interval()
    observe(Exception("YFRateLimitError: Too Many Requests. Rate limited."))
    assert current_interval() >= base * 2 - 1e-9, "429 未触发退避翻倍"
    hi = current_interval()
    for _ in range(200):
        observe(None)               # 连续成功缓降
    assert abs(current_interval() - _MIN) < 1e-6, f"未缓降回下限，仍为 {current_interval()}"
    assert not _is_rate_limit("connection reset"), "误判普通错误为限流"
    assert _is_rate_limit("HTTP 429"), "漏判 429"
    print(f"yf_gate 自检通过 (MIN={_MIN}s MAX={_MAX}s, 退避峰值={hi:.2f}s)")
