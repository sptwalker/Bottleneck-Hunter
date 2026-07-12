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


async def test_fred_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "bottleneck_hunter.data_provider.data_source_catalog.resolve_data_source_key",
        lambda src, *a, **k: "",
    )
    assert await macro_data._fetch_fred_indicators() == {}
