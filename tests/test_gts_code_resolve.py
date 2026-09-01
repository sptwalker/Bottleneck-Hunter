"""`_resolve_gts_code` 联网路径单测 —— 全 mock，不打真实网络。

背景：无精确前缀匹配时旧实现回退到「首个美股码项」，把 MU 静默映射到 MUZE.O
（美光 ADR，另一只证券），取到错误财务/行情。本组测试锁定「宁回退裸 ticker、
绝不借首个美股码造错码」的守卫行为。
精确匹配仍缓存；匹配失败不缓存（避免把「查无此股」永久缓存成错误码）。
"""

from __future__ import annotations

import pytest

from bottleneck_hunter.data_provider import gangtise_client as gc


class _FakeResp:
    def __init__(self, body: dict, status: int = 200):
        self._body = body
        self.status_code = status

    def json(self) -> dict:
        return self._body


def _search_body(items: list[dict]) -> dict:
    return {"code": "000000", "status": True, "data": {"list": items}}


def test_exact_prefix_match_maps_and_caches(monkeypatch):
    """精确前缀（gtsCode.rsplit('.')[0] == ticker）应映射到该码并进缓存。"""
    monkeypatch.setattr(gc, "_headers", lambda a, s: {})   # 跳过认证
    monkeypatch.setattr(gc.requests, "post", lambda *a, **k: _FakeResp(_search_body([
        {"gtsCode": "BABA"},
        {"gtsCode": "AAPL.O"},
        {"gtsCode": "AAPL.LON"},   # 非美股后缀 → 跳过
        {"gtsCode": "AAAPL.O"},    # 前缀 != ticker → 跳过
    ])))
    gc._gts_code_cache.clear()
    assert gc._resolve_gts_code("ak", "sk", "AAPL", "us_stock") == "AAPL.O"
    assert gc._gts_code_cache.get(("AAPL", "us_stock")) == "AAPL.O"   # 命中被缓存


def test_no_exact_match_returns_bare_ticker_no_cache(monkeypatch):
    """MU 案例：返回里全是 MULG.O/MUZE.O 等前缀不相同项 → 绝不取首个美股码。"""
    monkeypatch.setattr(gc, "_headers", lambda a, s: {})   # 跳过认证
    monkeypatch.setattr(gc.requests, "post", lambda *a, **k: _FakeResp(_search_body([
        {"gtsCode": "MUL.LON"},
        {"gtsCode": "MUZE.O"},   # 旧实现会错选它
        {"gtsCode": "MULG.O"},
        {"gtsCode": "MVIR.O"},
    ])))
    gc._gts_code_cache.clear()
    assert gc._resolve_gts_code("ak", "sk", "MU", "us_stock") == "MU"     # 裸 ticker
    assert gc._gts_code_cache.get(("MU", "us_stock")) is None             # 不缓存错误码


def test_non_us_code_skipped_then_bare_ticker(monkeypatch):
    """返回里只有非美股后缀项 → 同样拒绝，回退裸 ticker。"""
    monkeypatch.setattr(gc, "_headers", lambda a, s: {})   # 跳过认证
    monkeypatch.setattr(gc.requests, "post", lambda *a, **k: _FakeResp(_search_body([
        {"gtsCode": "MUL.LON"},
        {"gtsCode": "7292.TKS"},
    ])))
    gc._gts_code_cache.clear()
    assert gc._resolve_gts_code("ak", "sk", "MU", "us_stock") == "MU"


@pytest.mark.parametrize("code", [
    "AAPL.O",   # 已是美股码 → 原样直通，不联网
    "600519",   # A股 → 纯映射，不联网
])
def test_direct_passthrough(monkeypatch, code):
    def _boom(*a, **k):
        raise AssertionError("直通路径不应联网")
    monkeypatch.setattr(gc.requests, "post", _boom)
    if code == "AAPL.O":
        assert gc._resolve_gts_code("ak", "sk", code, "us_stock") == "AAPL.O"
    else:
        assert gc._resolve_gts_code("ak", "sk", code, "a_stock") == "600519.SH"
