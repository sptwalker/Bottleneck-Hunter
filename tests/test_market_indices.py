"""观察池顶栏市场指数：验证 yfinance 取不到时回落到快照 + 市场切换指标集正确。"""

import asyncio

from bottleneck_hunter.watchlist import macro_data


class _FakeStore:
    def __init__(self, snapshots):
        self._snaps = snapshots
        self.saved = []

    def get_latest_macro_snapshots(self):
        return self._snaps

    def save_macro_snapshot(self, indicator, date, value, fetched_at=None, change_pct=0.0):
        self.saved.append(indicator)


def _run(market, snapshots, monkeypatch):
    macro_data._index_bar_cache.clear()  # 避免跨用例 TTL 命中
    # 断网：强制走快照兜底，不打真实 yfinance
    monkeypatch.setattr(macro_data, "_fetch_yf_quote", lambda symbol: None)
    return asyncio.run(macro_data.fetch_market_indices(_FakeStore(snapshots), market))


def test_us_falls_back_to_snapshot(monkeypatch):
    snaps = [
        {"indicator": "sp500", "value": 5000.0, "change_pct": 1.2, "date": "2026-07-30", "fetched_at": "2026-07-30T20:00:00+00:00"},
        {"indicator": "vix", "value": 15.0, "change_pct": -0.5, "date": "2026-07-30", "fetched_at": "2026-07-30T20:00:00+00:00"},
    ]
    out = _run("us_stock", snaps, monkeypatch)
    keys = [i["key"] for i in out["indices"]]
    assert keys == ["sp500", "nasdaq", "vix"]
    sp = next(i for i in out["indices"] if i["key"] == "sp500")
    assert sp["value"] == 5000.0 and sp["stale"] is True
    nas = next(i for i in out["indices"] if i["key"] == "nasdaq")
    assert nas["value"] is None  # 无快照 → 空
    assert out["updated_at"] == "2026-07-30T20:00:00+00:00"


def test_a_stock_index_set(monkeypatch):
    out = _run("a_stock", [], monkeypatch)
    assert [i["key"] for i in out["indices"]] == ["sse_index", "szse_component", "csi500", "csi300"]
    assert out["updated_at"] is None and all(i["value"] is None for i in out["indices"])


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
