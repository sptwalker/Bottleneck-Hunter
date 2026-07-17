"""FRED 宏观改走共享 httpx 客户端(可借道)后，异步路径与解析仍正确（网络无关）。"""

from __future__ import annotations

import httpx

from bottleneck_hunter.watchlist import macro_data


def _handler(request):
    url = str(request.url)
    if "UNRATE" in url:
        obs = [{"date": "2026-06", "value": "4.1"}, {"date": "2026-05", "value": "4.0"}]
    elif "FEDFUNDS" in url:
        obs = [{"date": "2026-06", "value": "5.25"}, {"date": "2026-05", "value": "5.00"}]
    elif "CPIAUCSL" in url:  # 需 13 个月算同比，给 14 条
        obs = [{"date": f"m{i:02d}", "value": str(300 - i)} for i in range(14)]
    else:
        obs = []
    return httpx.Response(200, json={"observations": obs})


async def test_fred_indicators_via_shared_client(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    # _fred_series 内部 from retry import get_http_client → patch 那里
    monkeypatch.setattr("bottleneck_hunter.watchlist.retry.get_http_client", lambda timeout=20: client)
    # 让 FRED Key 解析出一个假 Key（否则无 Key 直接返回空）
    monkeypatch.setattr(
        "bottleneck_hunter.data_provider.data_source_catalog.resolve_data_source_key",
        lambda src, *a, **k: "FAKEKEY",
    )

    out = await macro_data._fetch_fred_indicators()
    await client.aclose()

    assert out["unemployment_rate"]["value"] == 4.1
    assert out["unemployment_rate"]["change_pct"] == 0.1   # 4.1 - 4.0
    assert out["fed_funds_rate"]["value"] == 5.25
    assert "cpi_yoy" in out and isinstance(out["cpi_yoy"]["value"], float)


def _handler_v2(request):
    """新增指标：10Y/2s10s/缩表(WALCL)/VIX/HY OAS/油/金。"""
    url = str(request.url)
    def obs(vals):
        return httpx.Response(200, json={"observations": [{"date": f"d{i}", "value": str(v)}
                                                           for i, v in enumerate(vals)]})
    if "DGS10" in url: return obs([4.28, 4.21])
    if "T10Y2Y" in url: return obs([-0.15, -0.10])           # 曲线倒挂 → 负值
    if "WALCL" in url: return obs([6612000, 6640000])        # 百万美元 → 6.612 万亿，周环比 -0.42%
    if "VIXCLS" in url: return obs([17.8, 19.2])
    if "BAMLH0A0HYM2" in url: return obs([3.05, 2.98])
    if "DCOILWTICO" in url: return obs([73.4, 71.9])
    return httpx.Response(200, json={"observations": []})


async def test_fred_new_indicators(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler_v2))
    monkeypatch.setattr("bottleneck_hunter.watchlist.retry.get_http_client", lambda timeout=20: client)
    monkeypatch.setattr(
        "bottleneck_hunter.data_provider.data_source_catalog.resolve_data_source_key",
        lambda src, *a, **k: "FAKEKEY",
    )
    out = await macro_data._fetch_fred_indicators()
    await client.aclose()

    assert out["us_10y_yield"]["value"] == 4.28
    assert out["yield_curve_2s10s"]["value"] == -0.15          # 倒挂负值保留
    assert out["fed_balance_sheet"]["value"] == 6.612          # 百万→万亿
    assert out["fed_balance_sheet"]["change_pct"] == -0.42     # 周环比%（缩表）
    assert out["vix"]["value"] == 17.8
    assert out["hy_oas"]["value"] == 3.05
    assert out["wti_oil"]["value"] == 73.4
    # 黄金已改走 akshare 上海金(FRED 伦敦金停更)，不在 FRED 结果内
    assert "gold" not in out


async def test_fred_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "bottleneck_hunter.data_provider.data_source_catalog.resolve_data_source_key",
        lambda src, *a, **k: "",
    )
    assert await macro_data._fetch_fred_indicators() == {}
