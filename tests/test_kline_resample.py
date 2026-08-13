"""_resample_ohlc 自检：日透传 / 周·月·年聚合 / 乱序 / 空。"""

from bottleneck_hunter.chain.financial_data import _resample_ohlc


def _bar(date, o, h, lo, c, v):
    return {"date": date, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def test_day_passthrough_and_empty():
    rows = [_bar("2026-08-10", 1, 2, 0.5, 1.5, 100)]
    assert _resample_ohlc(rows, "day") == rows          # 日线原样
    assert _resample_ohlc([], "week") == []             # 空输入不崩


def test_week_aggregation():
    # 2026-08-10(周一)~08-14(周五) 同一 ISO 周；08-17(下周一) 另一桶
    rows = [
        _bar("2026-08-10", 10, 12, 9, 11, 100),
        _bar("2026-08-11", 11, 15, 10, 13, 200),   # 本周最高 15
        _bar("2026-08-12", 13, 14, 8, 9, 150),     # 本周最低 8
        _bar("2026-08-14", 9, 11, 8.5, 10, 120),   # 本周末→close=10
        _bar("2026-08-17", 10, 20, 10, 18, 300),   # 下周
    ]
    out = _resample_ohlc(rows, "week")
    assert len(out) == 2
    w1 = out[0]
    assert w1["open"] == 10 and w1["close"] == 10          # 首开/末收
    assert w1["high"] == 15 and w1["low"] == 8             # 区间高低
    assert w1["volume"] == 100 + 200 + 150 + 120           # 量能求和
    assert w1["date"] == "2026-08-14"                       # 桶末日
    assert out[1]["open"] == 10 and out[1]["close"] == 18


def test_month_and_year_aggregation():
    rows = [
        _bar("2025-12-30", 5, 6, 4, 5.5, 10),
        _bar("2026-01-05", 6, 9, 5, 8, 20),
        _bar("2026-01-20", 8, 8.5, 3, 4, 30),      # 1 月最低 3
        _bar("2026-02-02", 4, 7, 3.5, 6, 40),
    ]
    months = _resample_ohlc(rows, "month")
    assert [m["date"][:7] for m in months] == ["2025-12", "2026-01", "2026-02"]
    jan = months[1]
    assert jan["open"] == 6 and jan["close"] == 4 and jan["high"] == 9 and jan["low"] == 3
    assert jan["volume"] == 50

    years = _resample_ohlc(rows, "year")
    assert [y["date"][:4] for y in years] == ["2025", "2026"]
    y26 = years[1]
    assert y26["open"] == 6 and y26["close"] == 6          # 2026 首开=1/5 的6, 末收=2/2 的6
    assert y26["high"] == 9 and y26["low"] == 3 and y26["volume"] == 90


def test_unsorted_input_still_correct():
    # 打乱顺序喂入，内部应先排序再聚合 → open 取真正最早日、close 取真正最晚日
    rows = [
        _bar("2026-01-20", 8, 8.5, 3, 4, 30),
        _bar("2026-01-05", 6, 9, 5, 8, 20),
        _bar("2026-01-12", 7, 7, 6, 6.5, 25),
    ]
    jan = _resample_ohlc(rows, "month")
    assert len(jan) == 1
    assert jan[0]["open"] == 6       # 1/5 最早
    assert jan[0]["close"] == 4      # 1/20 最晚
    assert jan[0]["high"] == 9 and jan[0]["low"] == 3


if __name__ == "__main__":
    for fn in [test_day_passthrough_and_empty, test_week_aggregation,
               test_month_and_year_aggregation, test_unsorted_input_still_correct]:
        fn()
    print("all ok")
