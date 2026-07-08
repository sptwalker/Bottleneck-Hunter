"""Provider 纯解析/字段映射离线自检 —— 喂样本响应断言映射，防上游改字段/量纲静默漏过。不打网。"""
from unittest.mock import patch

import bottleneck_hunter.data_provider.providers as P


def test_quarters_yoy():
    rows = [{"revenue_yi": 110, "net_profit_yi": 22}, {}, {}, {},
            {"revenue_yi": 100, "net_profit_yi": 20}]
    out = P._quarters_yoy(rows)
    assert out[0]["revenue_yoy_pct"] == 10.0
    assert out[0]["net_profit_yoy_pct"] == 10.0


def test_surprise_pct():
    assert P._surprise_pct(2.01, 1.95) == 3.08
    assert P._surprise_pct(None, 1.0) is None
    assert P._surprise_pct(1.0, 0) is None  # 除零保护


def test_f_scaling_and_strings():
    assert P._f("90000000000", 1e-8) == 900.0   # AV 字符串金额 → 亿
    assert P._f("None") is None
    assert P._f("-") is None
    assert P._f(None) is None


def test_alphavantage_financials_mapping():
    ov = {"Symbol": "AAPL", "LatestQuarter": "2026-03-31", "RevenueTTM": "400000000000",
          "ReturnOnEquityTTM": "1.2", "EPS": "6.5", "ForwardPE": "24.0", "PERatio": "31.0",
          "QuarterlyRevenueGrowthYOY": "0.05"}
    with patch.object(P, "_get_json_soft", return_value=ov):
        r = P.AlphaVantageProvider()._financials("AAPL", "k")
    assert r["revenue_yi"] == 4000.0          # 400e9 USD × 1e-8
    assert r["roe_pct"] == 120.0
    assert r["consensus_eps"] is None          # trailing EPS 不冒充一致预期
    assert r["consensus_pe"] == 24.0           # 只取 ForwardPE，不回退 trailing PERatio


def test_polygon_options_pcr():
    snap = {"results": [
        {"details": {"contract_type": "call", "strike_price": 100, "expiration_date": "2026-08-21"},
         "day": {"volume": 2000}, "open_interest": 5000},
        {"details": {"contract_type": "put", "strike_price": 90, "expiration_date": "2026-08-21"},
         "day": {"volume": 1000}, "open_interest": 3000},
    ]}
    with patch.object(P, "_get_json_soft", return_value=snap):
        r = P.PolygonProvider()._options("AAPL", "k")
    assert r["total_call_volume"] == 2000 and r["total_put_volume"] == 1000
    assert r["put_call_ratio"] == 0.5          # 1000/2000
    assert r["max_oi_strike"] == 100           # call OI 5000 > put 3000


def test_fmp_earnings_revenue_scaled_to_yi():
    rows = [{"epsActual": 2.01, "epsEstimated": 1.95,
             "revenueActual": 111184000000, "revenueEstimated": 109457600000, "date": "2026-04-30"}]
    with patch.object(P, "requests") as rq:
        rq.get.return_value.json.return_value = rows
        rq.get.return_value.raise_for_status.return_value = None
        r = P.FMPProvider()._fetch_earnings_sync("AAPL", "k")
    assert abs(r["revenue_actual"] - 1111.84) < 0.01   # USD → 亿
    assert r["eps_actual"] == 2.01


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("provider parse self-check OK")
