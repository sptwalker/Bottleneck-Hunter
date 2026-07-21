"""市场开市时间判断（北京时间口径）。

系统统一北京时区调度、且无实时逐笔行情，此判断仅用于「挂单轮询只在开市时段尝试成交」。
不处理节假日/半日市——闭市时价格快照不变，不会误成交，粗窗口足矣。

- A股：周一–周五 北京 09:30–15:00（含午休，不拆）。
- 美股：正常 09:30–16:00 ET。中国无夏令时，换算北京覆盖夏冬令时并集：
    夏令(EDT)=北京 21:30–次日04:00；冬令(EST)=北京 22:30–次日05:00。
    取并集 21:30–次日05:00 → 周一–五 21:30–23:59 或 周二–六 00:00–05:00。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_BJ = ZoneInfo("Asia/Shanghai")


def _m(h: int, mi: int) -> int:
    return h * 60 + mi


def is_market_open(market: str, now_bj: datetime | None = None) -> bool:
    """market='a_stock'|'us_stock'。now_bj 缺省取当前北京时间（可注入用于测试）。"""
    now = now_bj or datetime.now(_BJ)
    wd = now.weekday()  # 0=周一 .. 6=周日
    t = now.hour * 60 + now.minute
    if market == "a_stock":
        return wd < 5 and _m(9, 30) <= t <= _m(15, 0)
    # 美股（默认）：跨零点窗口
    if wd < 5 and t >= _m(21, 30):       # 周一–五 晚间段
        return True
    if 1 <= wd <= 5 and t <= _m(5, 0):   # 周二–六 凌晨段（对应美股周一–五盘）
        return True
    return False


if __name__ == "__main__":
    # ponytail: 自检——A股/美股窗口边界 + 周末闭市
    def bj(y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=_BJ)
    # 2026-07-22 是周三
    assert is_market_open("a_stock", bj(2026, 7, 22, 10, 0))      # A股盘中
    assert not is_market_open("a_stock", bj(2026, 7, 22, 16, 0))  # A股收盘后
    assert not is_market_open("a_stock", bj(2026, 7, 25, 10, 0))  # 周六
    assert is_market_open("us_stock", bj(2026, 7, 22, 22, 0))     # 周三晚=美股盘中
    assert is_market_open("us_stock", bj(2026, 7, 23, 3, 0))      # 周四凌晨=美股盘中
    assert not is_market_open("us_stock", bj(2026, 7, 22, 12, 0)) # 周三中午=美股闭市
    assert not is_market_open("us_stock", bj(2026, 7, 25, 22, 0)) # 周六晚：无盘
    assert is_market_open("us_stock", bj(2026, 7, 25, 3, 0))      # 周六凌晨=美股周五盘
    print("market_hours 自检通过")
