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
