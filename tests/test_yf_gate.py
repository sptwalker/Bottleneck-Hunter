"""yf_gate 全局限速闸门 —— 节流错峰 + 429 自适应退避。"""
import time

from bottleneck_hunter.data_provider import yf_gate


def test_is_rate_limit_detection():
    assert yf_gate._is_rate_limit("Too Many Requests. Rate limited.")
    assert yf_gate._is_rate_limit("HTTP 429")
    assert yf_gate._is_rate_limit(Exception("rate limited"))
    assert not yf_gate._is_rate_limit("connection reset by peer")
    assert not yf_gate._is_rate_limit("")


def test_observe_backoff_then_recover():
    yf_gate._reset()
    base = yf_gate.current_interval()
    yf_gate.observe(Exception("Too Many Requests"))
    assert yf_gate.current_interval() >= base * 2 - 1e-9, "429 应触发退避翻倍"
    for _ in range(300):
        yf_gate.observe(None)   # 连续成功缓降回下限
    assert abs(yf_gate.current_interval() - base) < 1e-6, "应缓降回 MIN"
    yf_gate._reset()


def test_backoff_capped_at_max():
    yf_gate._reset()
    for _ in range(100):
        yf_gate.observe(Exception("429"))
    assert yf_gate.current_interval() <= yf_gate._MAX + 1e-9, "退避不得超过上限"
    yf_gate._reset()


def test_throttle_spaces_calls(monkeypatch):
    yf_gate._reset()
    monkeypatch.setattr(yf_gate, "_interval", 0.1)
    yf_gate.throttle()               # 首次不等待（_next_at 在过去）
    t0 = time.monotonic()
    yf_gate.throttle()               # 第二次须等 ~0.1s
    dt = time.monotonic() - t0
    assert dt >= 0.08, f"节流未生效: {dt:.3f}s"
    yf_gate._reset()


def test_first_throttle_no_wait(monkeypatch):
    yf_gate._reset()
    monkeypatch.setattr(yf_gate, "_interval", 5.0)  # 大间隔但首发不该等
    t0 = time.monotonic()
    yf_gate.throttle()
    assert time.monotonic() - t0 < 0.5, "首次调用不应等待"
    yf_gate._reset()


def test_throttle_truncates_wait_at_budget(monkeypatch):
    """预算余量比下一槽还短时，闸门必须立刻抛 RateLimited，而不是照睡满间隔。

    这是「预算即最坏等待」的那一格：没有它，300s 的取数预算会被最后一个进门的调用
    拖成 327s（照睡满 30s 退避间隔）。
    """
    from bottleneck_hunter.chain import fetch_budget
    yf_gate._reset()
    monkeypatch.setattr(yf_gate, "_interval", 30.0)
    yf_gate.throttle()                      # 吃掉第一槽，把 _next_at 推到 30s 后
    fetch_budget.start(0.1)                 # 只剩 100ms 预算，睡不起这 30s
    try:
        t0 = time.monotonic()
        try:
            yf_gate.throttle()
            raise AssertionError("预算不足时 throttle 应抛 RateLimited")
        except yf_gate.RateLimited:
            pass
        assert time.monotonic() - t0 < 1.0, "预算不足时不应再睡"
    finally:
        fetch_budget.reset()
        yf_gate._reset()


def test_throttle_ignores_absent_budget(monkeypatch):
    """未开启预算（CLI/单测）时行为不变：照常按间隔节流。"""
    from bottleneck_hunter.chain import fetch_budget
    fetch_budget.reset()
    yf_gate._reset()
    monkeypatch.setattr(yf_gate, "_interval", 0.1)
    yf_gate.throttle()
    t0 = time.monotonic()
    yf_gate.throttle()
    assert time.monotonic() - t0 >= 0.08, "无预算时不应被截断"
    yf_gate._reset()


def test_concurrent_callers_get_no_deeper_than_one_interval(monkeypatch):
    """并发者不得被排到一个间隔之外——队列是节拍，不是积压。

    `_next_at` 会被每个调用者往后推，若不加封顶，起跑瞬间 N 个并发者依次拿到
    N × _interval 的等待（实测 9 个并发把第 9 个排到 45s，t=63s 时已排到 234s 之外），
    结果是失败迟迟不发生、熔断连续计数涨不上去，取数预算被拖穿。

    这里用「假 sleep」把等待记下来而不真睡：每个调用者报告自己排到的等待，最深的那个
    不得超过一个间隔。首发（_next_at 在过去）不睡，故 8 次调用记到 7 段等待。
    修前这 7 段是 0.5/1.0/…/3.5（最深 3.5s、合计 14.0s），修后应各为一个间隔。
    """
    from bottleneck_hunter.chain import fetch_budget
    slept: list[float] = []
    monkeypatch.setattr(yf_gate.time, "sleep", slept.append)
    fetch_budget.reset()
    yf_gate._reset()
    monkeypatch.setattr(yf_gate, "_interval", 0.5)
    try:
        for _ in range(8):      # 起跑瞬间 8 个并发者（复刻实测的突发入队）
            yf_gate.throttle()
        assert len(slept) == 7, f"首发不睡，应记到 7 段等待，实得 {slept}"
        deepest = max(slept)
        assert deepest <= 0.5 + 1e-6, f"最深排队 {deepest:.2f}s 超过一个间隔，退化为积压"
        assert sum(slept) <= 7 * 0.5 + 1e-6, f"总排队 {sum(slept):.2f}s 超过 7 × 间隔"
    finally:
        yf_gate._reset()
