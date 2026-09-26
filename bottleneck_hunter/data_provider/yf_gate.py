"""全局 Yahoo/yfinance 调用节流闸门 —— 把「一键刷新」的突发轰炸摊成均匀细流，避免 429/熔断。

**为什么是限速不是限并发**：Semaphore 只限「同时几个」，Yahoo 是按「多快打一次」限流的，
必须卡最小间隔。机构/分析师/新闻/期权/价格五条链此前只有 per-pipeline 的 Semaphore、零限速，
一只票 429 了立刻打下一只，持续硬撞——这是全表限流的根因（详见 institutional_pipeline 等）。

**为什么做成同步**：所有直连 yfinance 的取数都是同步函数、跑在 `asyncio.to_thread` 的线程池里。
同步闸门可以在每个取数函数开头一行 `throttle()` 接入，无需改动每层 async 包装。`time.sleep`
发生在工作线程里，不阻塞事件循环。全局单闸，五条链共享一条节流，互不越权。

**自适应**：`observe(err)` 反馈每次调用结果——撞到 429/限流就把间隔翻倍退避（上限 YF_MAX_INTERVAL），
连续成功再按 0.9 缓降回 YF_MIN_INTERVAL。这样限流一冒头就自动降速，风头过了自动提速。

**熔断（2026-09 加入）**：只退避不够——退避上限是 30s，而 Yahoo 若已对本机 IP 封禁，
退避只是把「立刻失败」换成「睡 30 秒再失败」。真实事故：2026-09 一次 Phase 2 入围筛选，
253 家候选逐个硬撞 429，338 次 × 中位 30s = 168.5 分钟全耗在 `time.sleep` 上，且结尾报告「0 失败」
（失败被吞）。故加连续失败计数：连续 `YF_TRIP_AFTER` 次、或连续限流持续 `YF_TRIP_WINDOW` 秒
即熔断，`throttle()` 改为**快速失败**抛 `RateLimited`（不再睡），让上层立刻走「无数据」降级分支；
冷却 `YF_COOLDOWN` 秒后放行**一次**探测请求，探通了自动复位。

时间口径是必需的：退避指数翻倍，等到第 20 次连续 429 时间隔早已顶到 30s，累计已白睡约 5 分钟
（实测冷启动 253 家全 429 一轮 = 537s，其中熔断前就占 327s）。有 `YF_TRIP_WINDOW` 封顶后，
最坏等待由「失败次数 × 当时的退避间隔」变成「窗口 + 冷却」，与候选数量无关。

调参（环境变量）：YF_MIN_INTERVAL 默认 3.0s、YF_MAX_INTERVAL 默认 30s、
YF_TRIP_AFTER 默认 20 次、YF_TRIP_WINDOW 默认 90s、YF_COOLDOWN 默认 900s。
"""
from __future__ import annotations

import os
import threading
import time

_MIN = float(os.environ.get("YF_MIN_INTERVAL", "3.0"))
_MAX = float(os.environ.get("YF_MAX_INTERVAL", "30"))
_TRIP_AFTER = int(os.environ.get("YF_TRIP_AFTER", "20"))      # 连续限流多少次后熔断
_TRIP_WINDOW = float(os.environ.get("YF_TRIP_WINDOW", "90"))  # 连续限流持续多少秒后熔断
_COOLDOWN = float(os.environ.get("YF_COOLDOWN", "900"))       # 熔断后多久放行一次探测（秒）

_lock = threading.Lock()
_next_at = 0.0          # 下一次允许发起调用的最早时刻（monotonic 时钟）
_interval = _MIN        # 当前自适应间隔

# ── 熔断状态 ──
_consecutive_429 = 0    # 连续限流计数（任何非限流结果即清零）
_tripped = False        # 是否已熔断
_tripped_at = 0.0       # 熔断时刻（monotonic）
_first_429_at = 0.0     # 本轮连续限流的起始时刻（monotonic），用于「持续时间」口径
_probe_inflight = False  # 探测请求是否已在途（冷却后只放一次）


class RateLimited(Exception):
    """闸门已熔断：Yahoo 对本机限流严重，请走「无数据」降级分支，勿再等待。

    上层（financial_data / smart_money 等）无需特别处理——它们是 `except Exception` 兜底，
    只是从「睡 30 秒后失败」变成「立刻失败」。
    """


def _budget_left() -> float | None:
    """本轮取数预算的剩余秒数；未开启预算（CLI/单测直接调用）时返回 None。

    延后导入避免 `chain.fetch_budget` ←→ `data_provider.yf_gate` 的循环引用；闸门只读它一个
    数值、不反向依赖链层，所以用函数内 import 而非模块级。用 `running()` 而非 `elapsed() > 0`：
    预算用完的那一刻 `elapsed()` 仍在增长，那样写会把「已超预算」误判成「还该睡」。
    """
    try:
        from bottleneck_hunter.chain import fetch_budget
    except ImportError:
        return None
    return fetch_budget.remaining() if fetch_budget.running() else None


def throttle() -> None:
    """在真正打 Yahoo 前调用：领一个均匀间隔的时间槽并睡到该槽。全局串行错峰。

    熔断期间立即抛 `RateLimited`（不睡），冷却期满后放行一次探测请求。

    若本轮已由 `fetch_budget` 设了取数总预算，等待时间会被**截到预算余量**：睡不起的槽就
    不再睡，直接抛 `RateLimited` 让调用方按「无数据」降级。没有这一层，预算只是一个「不再
    发起新请求」的软闸门——最后一个进门的调用仍会照睡满 30s 退避间隔，实测让 300s 的预算
    跑到 327s。这里截断的收益是「预算即最坏等待」。
    """
    global _next_at, _probe_inflight
    with _lock:
        if _tripped:
            if time.monotonic() - _tripped_at < _COOLDOWN:
                raise RateLimited(
                    f"Yahoo 限流熔断中（连续 {_consecutive_429} 次 429），"
                    f"冷却剩余 {_COOLDOWN - (time.monotonic() - _tripped_at):.0f}s"
                )
            if _probe_inflight:
                # 探测已在途：其余调用继续快速失败，不排队也不重复探测
                raise RateLimited("Yahoo 限流熔断中（探测请求已在途）")
            _probe_inflight = True   # 本次放行作为探测
        now = time.monotonic()
        left = _budget_left()
        # `_next_at` 是一条**向前的预约队列**：并发调用者依次把槽位往后推，第 N 个要等的是
        # N × _interval，而不是 _interval——它不受 _MAX 约束（_MAX 只封顶「单个间隔」）。
        # 实测：起跑瞬间 9 个并发者入队，第 9 个要等 45s；t=63s 那次已被排到 234s 之外。
        # 这不是「错峰」而是**积压**——失败要等到队尾才发生，熔断计数也永远涨不上去
        # （2026-09 那轮 253 次 429 里连续计数只到 3），于是 300s 的预算被拖成 297s+。
        # 故把队列**按本轮的节拍封顶**：一个间隔内只放行一个槽，排不到的调用立刻降级，
        # 而不是先睡一场再发现超时。代价是并发被削到与节拍一致——这正是「限速不是限并发」
        # 的本意：多出来的并发拿不到数据源，早点走「无数据」分支比睡在队里更有用。
        _next_at = min(_next_at, now + _interval)
        start = now if now >= _next_at else _next_at
        wait = start - now
        # 注意：**不要**在这里把 `_next_at` 再截到 `now + left`。那样 `wait` 会等于 `(now+left)-now`，
        # 即 `left` ±浮点噪声，下面这个严格 `>=` 就退化成抛硬币（实测同一权重下 `wait-left` 在
        # ±2e-11 间随 `now` 的量级变号）——丢了硬币就照睡满 `_interval`，正是本层要防的
        # 「300s 预算跑成 327s」。直接拿真实槽位等待与余量比：量级差 2~3 个数量级，无刀锋。
        if left is not None and wait >= left:
            # 不更新 _next_at：这一槽没被消费，额度留给未来真正还能跑的一轮
            raise RateLimited(f"取数预算已用尽（余 {left:.1f}s，需等 {wait:.1f}s）")
        _next_at = start + _interval
    if wait > 0:
        time.sleep(wait)   # 在 to_thread 的工作线程里睡，不碰事件循环


def observe(err: object | None = None) -> None:
    """调用结果反馈：err 命中 429/限流 → 间隔翻倍退避并累计连续失败；否则缓降回下限。

    连续限流到 `_TRIP_AFTER` 次**或**持续 `_TRIP_WINDOW` 秒即熔断；任何一次成功（含探测成功）
    复位熔断并清零计数。

    **为什么除次数外还要看持续时间**：退避是指数翻倍，第 20 次连续 429 到来之前间隔已经
    从 3s 涨到 30s 上限，累计已睡掉 ~5 分钟——2026-09 事故里 253 家候选正是这样「失败得
    太慢」。只按次数熔断，阈值定得越高，白等越久；加时间口径后，无论失败来得快还是慢，
    最坏等待都被 `_TRIP_WINDOW` 封顶。
    """
    global _interval, _consecutive_429, _tripped, _tripped_at, _first_429_at, _probe_inflight
    with _lock:
        if err is not None and _is_rate_limit(err):
            now = time.monotonic()
            if _consecutive_429 == 0:
                _first_429_at = now
            _interval = min(_MAX, max(_interval, _MIN) * 2.0)
            _consecutive_429 += 1
            if not _tripped and (_consecutive_429 >= _TRIP_AFTER
                                 or now - _first_429_at >= _TRIP_WINDOW):
                _tripped = True
                _tripped_at = now
        else:
            _interval = max(_MIN, _interval * 0.9)
            _consecutive_429 = 0
            _first_429_at = 0.0
            _tripped = False
            _tripped_at = 0.0
            _probe_inflight = False


def _is_rate_limit(err: object) -> bool:
    s = str(err).lower()
    return "too many requests" in s or "rate limited" in s or "429" in s


def current_interval() -> float:
    """当前自适应间隔（秒）——供健康面板/诊断读取。"""
    return _interval


def is_tripped() -> bool:
    """闸门是否处于熔断状态（批量入口据此跳过注定失败的第二轮重试）。"""
    with _lock:
        return _tripped


def snapshot() -> dict:
    """闸门当前状态快照——供健康面板/日志诊断一次性读取。"""
    with _lock:
        left = 0.0
        if _tripped:
            left = max(0.0, _COOLDOWN - (time.monotonic() - _tripped_at))
        return {
            "interval": round(_interval, 2),
            "consecutive_429": _consecutive_429,
            "tripped": _tripped,
            "cooldown_left": round(left, 1),
        }


def reset_for_new_run() -> None:
    """新的一次分析开始前调用：清熔断与连续失败计数，但**保留**自适应间隔。

    保留间隔是有意的——上一轮把间隔退避到 30s 说明 Yahoo 确实在限流，新一轮不该立刻回到 3s
    再撞一遍；间隔会随着成功调用按 0.9 自然缓降回去。
    """
    global _consecutive_429, _tripped, _tripped_at, _first_429_at, _probe_inflight
    with _lock:
        _consecutive_429 = 0
        _first_429_at = 0.0
        _tripped = False
        _tripped_at = 0.0
        _probe_inflight = False


def _reset() -> None:
    """仅供测试：清回初始状态。"""
    global _next_at, _interval
    with _lock:
        _next_at = 0.0
        _interval = _MIN
    reset_for_new_run()


if __name__ == "__main__":
    # ponytail: 自检——节流确实错峰、429 退避会翻倍、成功会缓降、熔断会快速失败并按时探测
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

    # ── 熔断：连续 429 到阈值即快速失败，冷却后放行一次探测 ──
    _reset()
    for _ in range(_TRIP_AFTER):
        observe(Exception("429 Too Many Requests"))
    assert is_tripped(), f"连续 {_TRIP_AFTER} 次 429 未熔断"
    assert not _probe_inflight
    t0 = time.monotonic()
    try:
        throttle()
        raise AssertionError("熔断期间 throttle() 应抛 RateLimited")
    except RateLimited:
        pass
    assert time.monotonic() - t0 < 0.5, "熔断期间应快速失败，不得睡退避间隔"

    # 连续 429 也不能把间隔顶破上限
    assert current_interval() <= _MAX + 1e-9, "退避不得超过上限"

    # 冷却期满：放行一次探测，其余调用仍快速失败
    # 注意用本文件的全局（= __main__ 的），勿 `import ... as _self`——那会加载出第二个模块副本
    with _lock:
        _tripped_at = time.monotonic() - _COOLDOWN - 1
        _next_at = 0.0
    throttle()                      # 探测被放行（不睡）
    assert _probe_inflight, "冷却后未放行探测"
    try:
        throttle()
        raise AssertionError("探测在途时其余调用应继续快速失败")
    except RateLimited:
        pass

    # 探测成功 → 完全复位
    observe(None)
    assert not is_tripped() and not _probe_inflight and _consecutive_429 == 0, "探测成功后未复位"

    # 非限流错误不该累计到熔断
    _reset()
    for _ in range(_TRIP_AFTER * 3):
        observe(Exception("connection reset by peer"))
    assert not is_tripped(), "普通网络错误被误判为限流并熔断"

    # ── 时间口径熔断：次数没到阈值，但连续限流持续够久也要熔断 ──
    # （否则退避翻倍会把「第 20 次」推迟到约 5 分钟后，等待随阈值线性变长）
    _reset()
    with _lock:
        _consecutive_429 = 3
        _first_429_at = time.monotonic() - _TRIP_WINDOW - 1
    observe(Exception("429 Too Many Requests"))
    assert is_tripped(), f"连续限流超过 {_TRIP_WINDOW:g}s 未熔断（仅按次数熔断会白等）"

    # 中途成功一次 → 本轮窗口重开，不因历史久远被误熔断
    _reset()
    with _lock:
        _first_429_at = time.monotonic() - _TRIP_WINDOW - 1
    observe(None)                       # 成功：清零计数与窗口
    assert not is_tripped() and _first_429_at == 0.0, "成功未重开限流窗口"
    observe(Exception("429 Too Many Requests"))   # 窗口从此刻重新起算
    assert not is_tripped(), f"窗口重开后仍按旧起点熔断（应为第 1 次而非超过 {_TRIP_WINDOW:g}s）"

    _reset()
    print(f"yf_gate 自检通过 (MIN={_MIN}s MAX={_MAX}s, 退避峰值={hi:.2f}s, "
          f"熔断={_TRIP_AFTER}次/{_TRIP_WINDOW:g}s, 冷却={_COOLDOWN:.0f}s)")
