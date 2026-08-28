"""系统守卫（silent-failure guard）自证测试 —— 纯逻辑 + 心跳盖章，不跑真 LLM。"""

from datetime import datetime, timedelta, timezone

import pytest

from bottleneck_hunter.watchlist import scheduler as sch
from bottleneck_hunter.watchlist.store import WatchlistStore


def _ago(days=0, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = WatchlistStore(tmp_path / "wd.db")
    monkeypatch.setattr(sch, "_wl_store", s)   # 守卫读全局 _wl_store
    return s


# ── 超期判定 ───────────────────────────────────────────────
class TestOverdue:
    def test_weekly_15d_overdue_but_3d_fresh(self):
        assert sch._is_overdue("weekly", _ago(days=15)) is True
        assert sch._is_overdue("weekly", _ago(days=3)) is False

    def test_daily_weekend_gap_not_overdue(self):
        # mon-fri 任务周五→周一自然 ~3 天间隔不应误报；≥~4 天才算
        assert sch._is_overdue("daily", _ago(days=3)) is False
        assert sch._is_overdue("daily", _ago(days=5)) is True

    def test_everyday_2d_overdue(self):
        assert sch._is_overdue("everyday", _ago(days=1)) is False
        assert sch._is_overdue("everyday", _ago(days=3)) is True

    def test_interval_threshold_is_3x(self):
        assert sch._overdue_threshold_hours("interval", 1) == 3.0
        assert sch._overdue_threshold_hours("interval", 6) == 18.0
        assert sch._is_overdue("interval", _ago(hours=2), interval_hours=1) is False
        assert sch._is_overdue("interval", _ago(hours=4), interval_hours=1) is True

    def test_no_heartbeat_is_overdue(self):
        assert sch._is_overdue("daily", None) is True
        assert sch._is_overdue("weekly", "") is True
        assert sch._is_overdue("daily", "not-a-date") is True


# ── 心跳盖章 round-trip ────────────────────────────────────
class TestHeartbeat:
    def test_stamp_then_read(self, store):
        sch._stamp_heartbeat("us_daily_decision", status="success")
        rows = {r["pipeline_name"]: r for r in store.get_pipeline_statuses()}
        assert "job:us_daily_decision" in rows
        r = rows["job:us_daily_decision"]
        assert r["last_status"] == "success"
        assert r["last_run_at"]   # success 应盖时间

    def test_running_does_not_stamp_time(self, store):
        sch._stamp_heartbeat("x_job", status="running")
        r = {row["pipeline_name"]: row for row in store.get_pipeline_statuses()}["job:x_job"]
        assert r["last_status"] == "running"
        assert not r["last_run_at"]   # running 不刷 last_run_at（避免污染"最近成功"）


# ── 扫描聚合：漏跑的周更被挑出，新鲜的日常不被挑 ──────────────
class TestScanOverdue:
    def test_stale_weekly_flagged_fresh_daily_not(self):
        schedule = {}   # interval 用默认 6h
        # 除 us_weekly_strategy 陈旧、cn_price_postmarket 报错外，其余全新鲜
        hb = {}
        for spec in sch._JOB_SPECS:
            jid = spec[0]
            hb[f"job:{jid}"] = {"pipeline_name": f"job:{jid}",
                                "last_run_at": _ago(hours=1), "last_status": "success"}
        hb["job:us_weekly_strategy"]["last_run_at"] = _ago(days=20)          # 漏跑 20 天
        hb["job:cn_price_postmarket"]["last_status"] = "error"               # 上次报错
        flagged = sch._scan_overdue(schedule, hb)
        assert "us_weekly_strategy" in flagged
        assert "cn_price_postmarket" in flagged
        assert "us_daily_decision" not in flagged
        assert "system_watchdog" not in flagged   # 守卫不查自己

    def test_empty_heartbeats_flag_all(self):
        flagged = sch._scan_overdue({}, {})
        ids = {s[0] for s in sch._JOB_SPECS if s[0] != "system_watchdog"}
        assert set(flagged) == ids   # 无任何心跳 = 全部从未跑过 = 全超期


# ── 报告白话拼装 ───────────────────────────────────────────
class TestReport:
    def test_report_mentions_all_sections(self):
        txt = sch._build_watchdog_report(
            overdue=["us_weekly_strategy"], caught_up=["us_weekly_strategy"],
            still_failed=[], stale_biz=["us_stock/abcd L2陈旧(20d)→重生并补决策"])
        assert "超期" in txt and "补跑" in txt and "陈旧" in txt

    def test_report_empty_is_normal(self):
        assert sch._build_watchdog_report([], [], [], []) == "巡检正常"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
