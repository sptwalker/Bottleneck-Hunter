"""反向分析身份核实：东财 stock_individual_info_em 被断连（生产 2026-09-28 实测对所有代码
RemoteDisconnected）时必须走腾讯行情兜底，而不是直接报「无法核实企业信息」。"""
from __future__ import annotations

import sys
import types

from bottleneck_hunter.chain.models import MarketRegion
from bottleneck_hunter.web.streaming import reverse


def _boom(**_):
    raise ConnectionError("Remote end closed connection without response")


def test_eastmoney_down_falls_back_to_tencent(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(stock_individual_info_em=_boom))
    monkeypatch.setattr("bottleneck_hunter.chain.supplier_search.fetch_tencent_quotes",
                        lambda codes: {"002334": {"name": "英威腾", "total_mcap_yi": 52.16}})
    out = reverse._fetch_company_basic("002334.SZ", MarketRegion.A_STOCK)
    assert out["name"] == "英威腾"
    assert out["market_cap"] == 52.16


def test_both_down_still_returns_empty_name(monkeypatch):
    """两源都失败 → name 仍为空，上游 fail-safe 照常报错，不臆测企业。"""
    monkeypatch.setitem(sys.modules, "akshare", types.SimpleNamespace(stock_individual_info_em=_boom))
    monkeypatch.setattr("bottleneck_hunter.chain.supplier_search.fetch_tencent_quotes", lambda codes: {})
    assert reverse._fetch_company_basic("002334.SZ", MarketRegion.A_STOCK)["name"] == ""
