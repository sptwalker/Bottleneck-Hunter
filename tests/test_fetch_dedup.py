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

