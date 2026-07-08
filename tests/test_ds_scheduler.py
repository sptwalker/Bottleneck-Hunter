"""数据源调度选择器自检 — 质量梯队 / 档内均衡 / 额度阀。不打网、不碰库。"""
import time

import bottleneck_hunter.data_provider.scheduler as sch


def setup_function():
    sch._reset_for_test()
    sch.set_store(None)


def test_intra_tier_balance():
    """同 priority 两源，按最近用量最少轮换 → A,B,A,B。"""
    a, b = ("srcA", 0), ("srcB", 0)
    picks = []
    for _ in range(4):
        top = sch.order([a, b])[0]
        picks.append(top)
        sch.note_call(top)  # 模拟真实调用累加负载
    assert picks == ["srcA", "srcB", "srcA", "srcB"], picks


def test_quality_tier_prefers_low_priority():
    """跨档：低 priority(高质量) 恒优先，不参与均衡。"""
    for _ in range(5):
        assert sch.order([("hi", 0), ("lo", 1)])[0] == "hi"
        sch.note_call("hi")


def test_per_min_quota_skips_and_recovers(monkeypatch):
    """per-min 超限的源被过滤 → 降级下一档；滚出窗口后恢复。"""
    monkeypatch.setenv("DS_QUOTA_POLYGON", "per_min=3")
    for _ in range(3):
        sch.note_call("polygon")
    # 超限 → polygon 被丢，只剩 yfinance
    assert sch.order([("polygon", 0), ("yfinance", 1)]) == ["yfinance"]
    assert sch.is_over_quota("polygon") is True
    # 把调用时刻推到 61s 前 → 滚出 per-min 窗口 → 恢复
    sch._recent["polygon"] = type(sch._recent["polygon"])(t - 61 for t in sch._recent["polygon"])
    assert sch.is_over_quota("polygon") is False
    assert sch.order([("polygon", 0), ("yfinance", 1)])[0] == "polygon"


def test_per_day_quota_from_store(monkeypatch):
    """per-day 用 DB 今日累计；超限即过滤。"""
    monkeypatch.setenv("DS_QUOTA_ALPHAVANTAGE", "per_day=20")

    class FakeStore:
        def get_ds_stats_by_source(self, days=1):
            return [{"source": "alphavantage", "calls": 20}]

    sch.set_store(FakeStore())
    assert sch.is_over_quota("alphavantage") is True
    assert sch.order([("alphavantage", 0), ("fmp", 1)]) == ["fmp"]


def test_free_source_never_throttled():
    """免费源不在额度表 → 恒不超额。"""
    for _ in range(1000):
        sch.note_call("yfinance")
    assert sch.is_over_quota("yfinance") is False


def test_cap_prio_backward_compat():
    """无 cap_priority 的 provider 落回 priority（老 provider 不受影响）。"""
    class OldProv:
        priority = 0

    class NewProv:
        priority = 2
        cap_priority = {"news": 0}

    assert sch.cap_prio(OldProv(), "earnings") == 0
    assert sch.cap_prio(NewProv(), "news") == 0
    assert sch.cap_prio(NewProv(), "earnings") == 2  # 未列出的能力落回 priority


if __name__ == "__main__":
    import sys
    setup_function()
    test_intra_tier_balance()
    test_quality_tier_prefers_low_priority()
    test_free_source_never_throttled()
    test_cap_prio_backward_compat()
    print("scheduler self-check OK")
    sys.exit(0)
