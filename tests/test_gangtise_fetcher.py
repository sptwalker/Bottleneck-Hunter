"""GangtiseFetcher（FetcherManager 行情路径最高优先层）单测 —— 全 mock，不打真实网络。

覆盖：market 推断 / 缺凭据降级 / OHLCV DataFrame 契约 + amount 透传 / tail 截取 /
实时 StandardQuote / 注册为严格最高档（-1，先于 efinance/yfinance）/ clean_ohlc A股量能归一联动。
"""

from __future__ import annotations

import pandas as pd
import pytest

from bottleneck_hunter.data_provider import gangtise_client as gc
from bottleneck_hunter.data_provider import data_source_catalog as dsc
from bottleneck_hunter.data_provider.base import StandardQuote
from bottleneck_hunter.data_provider.cleaning import clean_ohlc
from bottleneck_hunter.data_provider.fetchers.gangtise_fetcher import GangtiseFetcher


# ── market 推断（manager 不透传 market）─────────────────────────────
def test_infer_market():
    f = GangtiseFetcher._infer_market
    assert f("600519") == "a_stock"
    assert f("600519.SH") == "a_stock"
    assert f("000001.SZ") == "a_stock"
    assert f("AAPL") == "us_stock"
    assert f("AAPL.O") == "us_stock"
    assert f("BRK.B") == "us_stock"      # 字母类别码
    assert f("00700.HK") == "us_stock"   # 非 6 位数字 → 落 us（HK 不在 supported，无害）


# ── 缺凭据 → None（触发 fallback，绝不 raise）──────────────────────
async def test_fetch_daily_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: None)
    assert await GangtiseFetcher().fetch_daily("AAPL", 90) is None


async def test_fetch_realtime_no_creds_returns_none(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: None)
    assert await GangtiseFetcher().fetch_realtime("600519") is None


# ── 有凭据 + 数据 → DataFrame 契约（列齐全 + amount 透传）──────────
async def test_fetch_daily_contract(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: ("AK", "SK"))
    rows = [
        {"date": "2026-08-28", "open": 1490.0, "high": 1505.0, "low": 1485.0,
         "close": 1500.0, "volume": 29000.0, "amount": 4.3e7},
        {"date": "2026-08-29", "open": 1500.0, "high": 1520.0, "low": 1495.0,
         "close": 1512.0, "volume": 31000.0, "amount": 4.6e7},
    ]
    captured = {}

    def fake_ohlcv(ak, sk, ticker, market, days):
        captured.update(ak=ak, sk=sk, ticker=ticker, market=market, days=days)
        return rows

    monkeypatch.setattr(gc, "fetch_ohlcv_daily", fake_ohlcv)
    df = await GangtiseFetcher().fetch_daily("600519", 90)
    assert df is not None and list(df.columns) == ["date", "open", "high", "low", "close", "volume", "amount"]
    assert len(df) == 2 and df["close"].iloc[-1] == 1512.0
    assert df["amount"].iloc[-1] == 4.6e7          # amount 透传（clean_ohlc 量能反推依赖）
    assert captured["market"] == "a_stock" and captured["ticker"] == "600519"


async def test_fetch_daily_empty_returns_none(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: ("AK", "SK"))
    monkeypatch.setattr(gc, "fetch_ohlcv_daily", lambda *a, **k: [])
    assert await GangtiseFetcher().fetch_daily("AAPL", 90) is None


async def test_fetch_daily_tail_truncates(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: ("AK", "SK"))
    rows = [{"date": f"2026-08-{d:02d}", "open": 10.0, "high": 11.0, "low": 9.0,
             "close": 10.5, "volume": 100.0, "amount": 1050.0} for d in range(1, 11)]
    monkeypatch.setattr(gc, "fetch_ohlcv_daily", lambda *a, **k: rows)
    df = await GangtiseFetcher().fetch_daily("AAPL", 3)
    assert len(df) == 3 and df["date"].iloc[0] == "2026-08-08"   # 只留最后 3 根


# ── 实时 → StandardQuote ──────────────────────────────────────────
async def test_fetch_realtime_contract(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: ("AK", "SK"))
    monkeypatch.setattr(gc, "fetch_realtime_quote", lambda *a, **k: {
        "price": 232.5, "change_pct": 0.65, "volume": 5.2e7, "amount": 1.2e10})
    q = await GangtiseFetcher().fetch_realtime("AAPL")
    assert isinstance(q, StandardQuote) and q.source == "gangtise"
    assert q.price == 232.5 and q.change_pct == 0.65 and q.volume == 52000000


async def test_fetch_realtime_zero_price_returns_none(monkeypatch):
    monkeypatch.setattr(dsc, "resolve_gangtise_credentials", lambda *a, **k: ("AK", "SK"))
    monkeypatch.setattr(gc, "fetch_realtime_quote", lambda *a, **k: {"price": 0.0})
    assert await GangtiseFetcher().fetch_realtime("AAPL") is None


# ── 注册：严格最高档（-1），先于 efinance/yfinance ────────────────
def test_registered_as_strict_top():
    from bottleneck_hunter.data_provider import _create_manager
    mgr = _create_manager()
    status = mgr.get_status()  # 已按 priority 升序
    names = [s["name"] for s in status]
    assert "gangtise" in names, names
    g = next(s for s in status if s["name"] == "gangtise")
    assert g["priority"] == -1, g
    assert names[0] == "gangtise", names                     # 全局最前
    assert set(g["markets"]) == {"a_stock", "us_stock"}, g
    # 两链内均先于对应免费源
    for peer in ("efinance", "yfinance"):
        if peer in names:
            assert names.index("gangtise") < names.index(peer), (peer, names)


# ── 联动：A股经 manager 的 clean_ohlc 量能归一（amount 反推「手」）──
def test_clean_ohlc_normalizes_ashare_volume_via_amount():
    # Gangtise A股 volume 若以「股」计（r=amount/(close·vol)≈1），clean_ohlc 应 ÷100 归一到「手」
    df = pd.DataFrame([
        {"date": "2026-08-2%d" % d, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0,
         "volume": 1_000_000.0, "amount": 1.0e7} for d in range(1, 6)
    ])
    out = clean_ohlc(df, "gangtise", "a_stock")
    assert out is not None
    # r = 1e7/(10·1e6)=1 → 判「股」→ ÷100 → 10000「手」
    assert int(out["volume"].iloc[-1]) == 10_000, out["volume"].tolist()
