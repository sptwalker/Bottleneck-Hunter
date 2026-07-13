"""时区统一 + 调度修复回归测试。

- 所有定时任务触发器均为 Asia/Shanghai（无 America/New_York）。
- 美股关键任务的北京时刻全年落在正确位置（收盘后/开盘前，夏冬两种偏移都验证）。
- 数据源巡检覆盖周末（everyday）。
- list_job_labels / list_job_categories 覆盖全部 _JOB_SPECS。
- 全局总开关关闭时 macro/datasource/calibration 早退。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bottleneck_hunter.watchlist import scheduler as sch
from bottleneck_hunter.watchlist.schedule_config import get_global_schedule


def _triggers():
    schedule = get_global_schedule(None)
    return {spec[0]: sch._make_trigger(spec, schedule) for spec in sch._JOB_SPECS}


def test_all_triggers_beijing_no_us_tz():
    for job_id, trig in _triggers().items():
        tz = str(getattr(trig, "timezone", ""))
        # interval trigger 无 timezone 属性 → 跳过
        if tz:
            assert tz == "Asia/Shanghai", f"{job_id} tz={tz}"


def _next_fire_utc(trig, after_local):
    """给定北京时刻，算触发器下一次触发（返回 UTC datetime）。"""
    nxt = trig.get_next_fire_time(None, after_local)
    return nxt.astimezone(ZoneInfo("UTC"))


def test_us_postmarket_after_us_close_year_round():
    """us_price_postmarket(北京05:30) 的 UTC 触发点须晚于美股收盘。
    美股收盘 16:00 ET = 21:00 UTC(冬 EST) / 20:00 UTC(夏 EDT)。北京05:30 = 前一日21:30 UTC。
    等价：北京 05:30 对应 UTC 21:30，>21:00(冬)且>20:00(夏)，全年晚于收盘。"""
    trig = _triggers()["us_price_postmarket"]
    bj = ZoneInfo("Asia/Shanghai")
    for probe in (datetime(2026, 1, 15, 0, 0, tzinfo=bj), datetime(2026, 7, 15, 0, 0, tzinfo=bj)):
        fire_utc = _next_fire_utc(trig, probe)
        # 北京05:30 == UTC 前一日 21:30
        assert (fire_utc.hour, fire_utc.minute) == (21, 30), fire_utc
        # 收盘上界 21:00 UTC（冬），05:30 北京晚于它
        assert fire_utc.hour * 60 + fire_utc.minute >= 21 * 60, fire_utc


def test_us_premarket_before_us_open_year_round():
    """us_price_premarket(北京21:00) 须早于美股开盘。
    开盘 09:30 ET = 14:30 UTC(冬) / 13:30 UTC(夏)。北京21:00 = 13:00 UTC，早于两者。"""
    trig = _triggers()["us_price_premarket"]
    bj = ZoneInfo("Asia/Shanghai")
    for probe in (datetime(2026, 1, 15, 12, 0, tzinfo=bj), datetime(2026, 7, 15, 12, 0, tzinfo=bj)):
        fire_utc = _next_fire_utc(trig, probe)
        assert (fire_utc.hour, fire_utc.minute) == (13, 0), fire_utc
        assert fire_utc.hour < 13 or (fire_utc.hour == 13 and fire_utc.minute <= 30), fire_utc


def test_datasource_report_covers_weekend():
    """数据源巡检 everyday：周六也应触发。"""
    trig = _triggers()["datasource_report"]
    bj = ZoneInfo("Asia/Shanghai")
    # 2026-07-11 是周六，从周五午夜起算下一次触发应在周六
    fri = datetime(2026, 7, 10, 23, 0, tzinfo=bj)
    nxt = trig.get_next_fire_time(None, fri).astimezone(bj)
    assert nxt.weekday() in (5, 4), nxt  # 周五当天07:30 或周六07:30，都属含周末覆盖
    # 明确验证周六能触发（从周六 00:00 起）
    sat = datetime(2026, 7, 11, 0, 0, tzinfo=bj)
    nxt_sat = trig.get_next_fire_time(None, sat).astimezone(bj)
    assert nxt_sat.weekday() == 5, nxt_sat


def test_labels_and_categories_cover_all_specs():
    ids = {spec[0] for spec in sch._JOB_SPECS}
    assert ids - set(sch.list_job_labels()) == set()
    assert ids - set(sch.list_job_categories()) == set()


@pytest.mark.asyncio
async def test_global_kill_switch_stops_macro_and_datasource(monkeypatch):
    """全局总开关关闭时，macro/datasource/calibration 应早退不动数据。"""
    import bottleneck_hunter.watchlist.schedule_config as sc
    monkeypatch.setattr(sc, "is_global_enabled", lambda _store: False)

    # 注入一个会记录调用的假 store；若任务未早退会触碰它
    touched = {"n": 0}

    class _FakeStore:
        def __getattr__(self, _name):
            touched["n"] += 1
            raise AssertionError("job 未早退，触碰了 store")

    monkeypatch.setattr(sch, "_wl_store", _FakeStore())
    monkeypatch.setattr(sch, "_auth_store", object())

    await sch.job_macro_update()
    await sch.job_datasource_report()
    await sch.job_model_calibration()
    assert touched["n"] == 0


if __name__ == "__main__":
    test_all_triggers_beijing_no_us_tz()
    test_us_postmarket_after_us_close_year_round()
    test_us_premarket_before_us_open_year_round()
    test_datasource_report_covers_weekend()
    test_labels_and_categories_cover_all_specs()
    print("PASS (sync checks)")
