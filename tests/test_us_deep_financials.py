"""`_fetch_us_deep_financials` 采集/紧凑映射自检（不打真 FMP，monkeypatch 桩）：

- 有 key + FMPProvider 返回完整 → 紧凑成焦点块所需字段，单位/改名正确，剔冗余
- 无 key → {}（上层维持既有 profile，绝不覆盖）

真 FMP 拉取只能在用户服务器带真 key 验收；本测覆盖映射逻辑，不覆盖网络。
"""
import asyncio

from bottleneck_hunter.data_provider import data_source_catalog, providers
from bottleneck_hunter.watchlist import price_pipeline


def test_deep_financials_compact(monkeypatch):
    monkeypatch.setattr(data_source_catalog, "resolve_data_source_key", lambda src: "fake-key")
    fake = {"data_source": "fmp", "report_date": "2026-06-30",
            "revenue_yi": 130.5, "revenue_yoy_pct": 12.3,
            "net_profit_yi": 4.2, "net_profit_yoy_pct": -96.0,
            "gross_margin_pct": 74.8, "roe_pct": 55.1, "debt_ratio_pct": 25.0,
            "cashflow_per_share": 3.1, "consensus_eps": 1.0, "analyst_report_count": 30,
            "quarters": [{"report_date": "2026-06-30", "revenue_yi": 130.5, "net_profit_yi": 4.2,
                          "gross_margin_pct": 74.8, "net_profit_yoy_pct": -96.0}]}
    monkeypatch.setattr(providers.FMPProvider, "_fetch_financials_sync", lambda self, t, k: fake)
    out = asyncio.run(price_pipeline._fetch_us_deep_financials("SNPS"))
    assert out["net_profit_yoy_pct"] == -96.0          # 可验证"净利暴跌96%"
    assert out["debt_to_equity_pct"] == 25.0           # debt_ratio_pct → debt_to_equity_pct
    assert out["operating_cf_per_share"] == 3.1        # cashflow_per_share → operating_cf_per_share
    assert out["revenue_yi"] == 130.5 and out["unit"] == "亿美元/百分比"
    assert len(out["quarters"]) == 1 and out["quarters"][0]["date"] == "2026-06-30"
    assert "consensus_eps" not in out and "analyst_report_count" not in out   # 只留焦点块紧凑字段


def test_deep_financials_no_key(monkeypatch):
    monkeypatch.setattr(data_source_catalog, "resolve_data_source_key", lambda src: "")
    assert asyncio.run(price_pipeline._fetch_us_deep_financials("SNPS")) == {}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
