"""FRED 股指兜底的 change_pct 必须是百分比，而非绝对点差（否则纳指会显示 -433.97%）。"""

import asyncio

from bottleneck_hunter.watchlist import macro_data


def test_equity_change_pct_is_percentage(monkeypatch):
    # 纳斯达克：24442.94(今) vs 24876.91(昨) → 真实 ≈ -1.74%，绝对点差会得出 -433.97
    obs = {
        "SP500": [{"value": "7316.15", "date": "2026-07-31"}, {"value": "7428.78", "date": "2026-07-30"}],
        "NASDAQCOM": [{"value": "24442.94", "date": "2026-07-31"}, {"value": "24876.91", "date": "2026-07-30"}],
    }
    import bottleneck_hunter.data_provider.data_source_catalog as cat
    monkeypatch.setattr(cat, "resolve_data_source_key", lambda _src: "fake-key")

    async def _fake_series(key, series_id, limit=1):
        return obs.get(series_id, [])[:limit]

    monkeypatch.setattr(macro_data, "_fred_series", _fake_series)
    out = asyncio.run(macro_data._fetch_fred_indicators(extra=macro_data._FRED_US_EQUITY))

    nas = out["nasdaq"]
    assert nas["value"] == 24442.94
    assert -3.0 < nas["change_pct"] < 0.0, f"应为百分比(≈-1.74)，实际 {nas['change_pct']}"
    assert out["sp500"]["change_pct"] < 0.0 and out["sp500"]["change_pct"] > -3.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
