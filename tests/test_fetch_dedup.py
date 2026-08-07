"""举一反三：全系统数据拉取去重/冷却/并发 回归。

对标 sec_pipeline 的「重拉已存不变数据 + 串行让信号量形同虚设」两类问题，
覆盖 options 同日跳过、price 档案冷却、13F 冷却、macro 同日短路、batch 并发契约。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "t.db")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── options: 当日已抓过整条链 → 跳过 hub ────────────────────────────
def test_options_same_day_skip(store, monkeypatch):
    from bottleneck_hunter.watchlist import options_pipeline as op
    store.save_options([{
        "id": "x", "ticker": "AAPL", "date": _today(),
        "unusual_volume": False, "put_call_ratio": None,
        "total_call_volume": 0, "total_put_volume": 0, "fetched_at": _now_iso(),
    }])
    import bottleneck_hunter.data_provider.hub as hub

    def _boom(*a, **k):
        raise AssertionError("hub 不应被调用")

    monkeypatch.setattr(hub, "get_hub", _boom)
    assert asyncio.run(op._fetch_one("AAPL", store)) == "cached"


def test_options_batch_concurrent(monkeypatch):
    from bottleneck_hunter.watchlist import options_pipeline as op

    async def fake(t, store):
        return "ok"

    monkeypatch.setattr(op, "_fetch_one", fake)
    res = asyncio.run(op.fetch_options_batch(["A", "B", "C"], store=object()))
    assert res == {"A": "ok", "B": "ok", "C": "ok"}


# ── notice: 并发 batch 仍保留 per-ticker 错误映射 ──────────────────
def test_notice_batch_error_mapped(monkeypatch):
    from bottleneck_hunter.watchlist import notice_pipeline as npipe

    async def boom(t, store, cache=None):
        raise RuntimeError("x")

    monkeypatch.setattr(npipe, "_fetch_one", boom)
    res = asyncio.run(npipe.fetch_notice_batch(["600519"], store=object()))
    assert res["600519"]["filings"] == -1


# ── price: 公司档案 24h 冷却 ────────────────────────────────────────
def test_profile_fresh(store):
    from bottleneck_hunter.watchlist import price_pipeline as pp
    store.save_company_profile("AAPL", {"sector": "Tech", "industry": "HW"})
    assert pp._profile_fresh(store, "AAPL") is True
    store.save_company_profile("EMPT", {})           # 空 stub 不算新鲜
    assert pp._profile_fresh(store, "EMPT") is False
    assert pp._profile_fresh(store, "NOPE") is False
    conn = store._connect()
    try:
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
        conn.execute("UPDATE company_profiles SET fetched_at=? WHERE ticker='AAPL'", (old,))
        conn.commit()
    finally:
        conn.close()
    assert pp._profile_fresh(store, "AAPL") is False  # 超冷却窗 → 允许重拉


# ── price: 429 伪成功字典绝不覆盖真档案 (生产"全部空缺"根因) ─────────
def test_profile_garbage_never_wipes_real(store):
    store.save_company_profile("NVDA", {
        "sector": "Tech", "industry": "Semis", "longBusinessSummary": "GPU maker",
    })
    # 模拟 429 伪成功：truthy 但只含噪声、无任何身份/财务字段
    store.save_company_profile("NVDA", {"trailingPegRatio": None, "maxAge": 86400})
    prof = store.get_company_profile("NVDA")
    assert prof["sector"] == "Tech"          # 真资料保留、未被抹空
    assert prof["industry"] == "Semis"
    assert prof["description"] == "GPU maker"


def test_profile_astock_financial_only_saved(store):
    """A股仅财务(baostock，无 sector/行业)也算真内容，须落库(不被误判为空)。"""
    store.save_company_profile("600519", {
        "trailingPE": 30.5, "priceToBook": 9.1, "currency": "CNY", "country": "中国",
    })
    prof = store.get_company_profile("600519")
    assert prof is not None
    assert prof["currency"] == "CNY"
    assert prof["raw"].get("trailingPE") == 30.5


def test_profile_is_stale_empty_stub_shorter_window():
    """on-demand 冷却窗按内容分档：空 stub 2h 即可重试，真档案 24h。"""
    from bottleneck_hunter.web.watchlist_api import _profile_is_stale
    now = datetime.now(timezone.utc)
    ts_3h = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    # 空 stub 3h 前抓的 → 过 2h 窗 → 允许重拉(自愈被抹空档案)
    assert _profile_is_stale({"sector": "", "industry": "", "description": "", "fetched_at": ts_3h}, now) is True
    # 真档案 3h 前抓的 → 未过 24h 窗 → 新鲜、不重拉
    assert _profile_is_stale({"sector": "Tech", "fetched_at": ts_3h}, now) is False


# ── price: FMP 代理源(借道白名单)映射 + 无 Key 回退 ───────────────
class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload):
        self._p = payload
        self.calls = []
    async def get(self, url, **kw):
        self.calls.append(url)
        return _FakeResp(self._p)


def _patch_fmp(monkeypatch, key, payload):
    import bottleneck_hunter.data_provider.data_source_catalog as cat
    import bottleneck_hunter.watchlist.retry as retry
    monkeypatch.setattr(cat, "resolve_data_source_key", lambda sid, user_id="": key if sid == "fmp" else "")
    client = _FakeClient(payload)
    monkeypatch.setattr(retry, "get_http_client", lambda *a, **k: client)
    return client


def test_fmp_profile_maps_to_yfinance_keys(monkeypatch):
    from bottleneck_hunter.watchlist import price_pipeline as pp
    client = _patch_fmp(monkeypatch, "K", [{
        "symbol": "AAPL", "companyName": "Apple Inc.", "sector": "Technology",
        "industry": "Consumer Electronics", "description": "Designs phones.",
        "website": "https://apple.com", "fullTimeEmployees": "164,000",
        "country": "US", "exchangeShortName": "NASDAQ", "currency": "USD",
        "mktCap": 3.5e12, "pe": 30.1, "ceo": "Tim Cook",
    }])
    info = asyncio.run(pp._fetch_company_info_fmp("aapl"))
    assert "financialmodelingprep.com/stable/profile?symbol=AAPL" in client.calls[0]
    assert info["sector"] == "Technology"
    assert info["industry"] == "Consumer Electronics"
    assert info["longBusinessSummary"] == "Designs phones."
    assert info["marketCap"] == 3.5e12
    assert info["exchange"] == "NASDAQ"
    assert info["fullTimeEmployees"] == 164000     # 逗号已剥、转 int
    assert info["longName"] == "Apple Inc."
    assert info["trailingPE"] == 30.1
    assert info["companyOfficers"][0]["name"] == "Tim Cook"
    # 该 dict 被 save_company_profile 认作真内容(不会当空 stub)
    from bottleneck_hunter.watchlist.store_market_data import _profile_has_content
    assert _profile_has_content(info) is True


def test_fmp_profile_no_key_returns_empty(monkeypatch):
    """用户未配 FMP Key → 返 {}，由 _fetch_one 回退 yfinance(铁律:绝不借他人/全局 Key)。"""
    import bottleneck_hunter.data_provider.data_source_catalog as cat
    import bottleneck_hunter.watchlist.retry as retry
    from bottleneck_hunter.watchlist import price_pipeline as pp
    monkeypatch.setattr(cat, "resolve_data_source_key", lambda sid, user_id="": "")
    # get_http_client 不应被触及(无 Key 提前返回)
    monkeypatch.setattr(retry, "get_http_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("无 Key 不应发请求")))
    assert asyncio.run(pp._fetch_company_info_fmp("AAPL")) == {}


def test_fmp_domain_in_borrow_whitelist():
    """FMP 域名必须在借道白名单里，否则不走桌面隧道、国内不可达。"""
    from bottleneck_hunter.web.egress_relay import is_borrow_domain
    assert is_borrow_domain("financialmodelingprep.com") is True


def test_fetch_one_us_fmp_first_then_yf_fallback(store, monkeypatch):
    """美股 _fetch_one：FMP 有内容则用 FMP、不打 yfinance；FMP 空则回退 yfinance。"""
    from bottleneck_hunter.watchlist import price_pipeline as pp

    async def _no_snaps(*a, **k):
        return []
    monkeypatch.setattr(pp, "_fetch_via_manager", _no_snaps)
    # 行情直连也返空(只测 profile 分支)
    monkeypatch.setattr(pp, "_fetch_daily_data", lambda t, d: ([], {}))

    yf_called = []
    monkeypatch.setattr(pp, "_fetch_company_info_us",
                        lambda t: yf_called.append(t) or {"sector": "YF", "industry": "fallback"})

    # 情形①：FMP 有内容 → 用 FMP，不触 yfinance
    async def _fmp_ok(t):
        return {"sector": "FMP", "industry": "primary", "longBusinessSummary": "x"}
    monkeypatch.setattr(pp, "_fetch_company_info_fmp", _fmp_ok)
    asyncio.run(pp._fetch_one("AAA", store, market="us_stock"))
    assert store.get_company_profile("AAA")["sector"] == "FMP"
    assert yf_called == []

    # 情形②：FMP 空 → 回退 yfinance
    async def _fmp_empty(t):
        return {}
    monkeypatch.setattr(pp, "_fetch_company_info_fmp", _fmp_empty)
    asyncio.run(pp._fetch_one("BBB", store, market="us_stock"))
    assert store.get_company_profile("BBB")["sector"] == "YF"
    assert yf_called == ["BBB"]


# ── institutional: 13F 30 天冷却 ───────────────────────────────────
def test_holders_fresh(store):
    from bottleneck_hunter.watchlist import institutional_pipeline as ip
    store.save_institutional_holders("AAPL", [{
        "holder_name": "Vanguard", "shares": 100, "value": 1.0,
        "pct_held": 5.0, "date": "2026-08-01", "fetched_at": _now_iso(),
    }])
    assert ip._holders_fresh(store, "AAPL") is True
    assert ip._holders_fresh(store, "NOPE") is False


# ── macro: 同日指标采齐 → 零网络短路 ───────────────────────────────
def test_macro_same_day_shortcircuit(store, monkeypatch):
    from bottleneck_hunter.watchlist import macro_data as m
    keys = [k for k, _s, _l in (m._GLOBAL_INDICATORS + m._US_INDICATORS)]
    for k in keys:
        store.save_macro_snapshot(k, _today(), 1.23, _now_iso(), change_pct=0.5)

    called: list = []
    monkeypatch.setattr(m, "_fetch_yf_quote", lambda s: called.append(s) or {"value": 9.9})

    async def _no_fred(extra=None):
        called.append("fred")
        return {}

    monkeypatch.setattr(m, "_fetch_fred_indicators", _no_fred)
    res = asyncio.run(m.fetch_macro_data(store, ["us_stock"]))
    assert not called                       # 同日已采齐 → 零网络
    assert all(k in res for k in keys)       # 返回库里的值


# ── macro: 同日短路不得把他市专属指标串味进本市场 ─────────────────
def test_macro_same_day_no_cross_market_leak(store, monkeypatch):
    from bottleneck_hunter.watchlist import macro_data as m
    # 先灌满 us_stock 今日应采的全部 yfinance 指标(触发短路探针)
    for k, _s, _l in (m._GLOBAL_INDICATORS + m._US_INDICATORS):
        store.save_macro_snapshot(k, _today(), 1.0, _now_iso())
    # 再灌一个 a_stock 专属指标(北向资金)——同在全局表、同今日
    store.save_macro_snapshot("northbound_flow", _today(), 88.0, _now_iso())

    monkeypatch.setattr(m, "_fetch_yf_quote", lambda s: {"value": 9.9})

    async def _no_fred(extra=None):
        return {}

    monkeypatch.setattr(m, "_fetch_fred_indicators", _no_fred)
    res = asyncio.run(m.fetch_macro_data(store, ["us_stock"]))
    assert "northbound_flow" not in res      # a股专属不得串进美股 L1 宏观口径

