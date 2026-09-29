"""数据时效门测试 —— _ensure_price_freshness 的三条判定路径。

覆盖用户明确的两条边界：
- 更新失败硬停：过半 error → halt['stop']=True + data_refresh_block
- 部分失败阈值：仅过半才算失败；个别票 error/no_data 不硬停
- 全新鲜跳过：无过期票 → data_freshness_pass，不触发抓取
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from bottleneck_hunter.watchlist.decision_engine import _ensure_price_freshness
from bottleneck_hunter.watchlist.store import WatchlistStore


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _snap(ticker: str, days_ago: int) -> dict:
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date().isoformat()
    return {"ticker": ticker, "date": d, "close": 100.0, "fetched_at": _iso(days_ago),
            "market": "us_stock"}


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(tmp_path / "fresh.db")
    for t in ("AAA", "BBB", "CCC", "DDD"):
        s.add({"ticker": t, "company_name": t, "market": "us_stock", "tier": "focus"})
    return s


async def _collect(store, market, halt, results=None, raise_exc=False):
    fake = AsyncMock(side_effect=Exception("net down")) if raise_exc else AsyncMock(return_value=results or {})
    with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", fake):
        evts = [e async for e in _ensure_price_freshness(store, market, halt)]
    return evts, fake


@pytest.mark.asyncio
async def test_all_fresh_skips_refresh(store):
    for t in ("AAA", "BBB", "CCC", "DDD"):
        store.save_snapshots([_snap(t, 0)])  # 今天抓的 → green
    halt = {}
    evts, fake = await _collect(store, "us_stock", halt)
    assert not halt.get("stop")
    assert any(e["event"] == "data_freshness_pass" for e in evts)
    fake.assert_not_called()  # 全新鲜绝不触发抓取


@pytest.mark.asyncio
async def test_majority_fail_hard_stops(store):
    for t in ("AAA", "BBB", "CCC", "DDD"):
        store.save_snapshots([_snap(t, 10)])  # 10 天前 → red，全过期
    halt = {}
    # 4 票尝试更新，3 票 error（过半）→ 硬停
    results = {"AAA": "error: 502", "BBB": "error: timeout", "CCC": "error: dns", "DDD": "ok"}
    evts, _ = await _collect(store, "us_stock", halt, results=results)
    assert halt.get("stop") is True
    assert any(e["event"] == "data_refresh_block" for e in evts)


@pytest.mark.asyncio
async def test_minority_fail_continues(store):
    for t in ("AAA", "BBB", "CCC", "DDD"):
        store.save_snapshots([_snap(t, 10)])
    halt = {}
    # 仅 1 票 error（未过半）+ 1 no_data → 继续，不硬停
    results = {"AAA": "ok", "BBB": "ok", "CCC": "error: delisted", "DDD": "no_data"}
    evts, _ = await _collect(store, "us_stock", halt, results=results)
    assert not halt.get("stop")
    assert any(e["event"] == "data_refresh_done" for e in evts)


@pytest.mark.asyncio
async def test_fetch_raises_hard_stops(store):
    store.save_snapshots([_snap("AAA", 10)])
    halt = {}
    evts, _ = await _collect(store, "us_stock", halt, raise_exc=True)
    assert halt.get("stop") is True
    assert any(e["event"] == "data_refresh_block" for e in evts)


if __name__ == "__main__":  # 便于 GBK 控制台直接 python 跑
    import asyncio

    async def _main():
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            s = WatchlistStore(Path(d) / "m.db")
            for t in ("AAA", "BBB", "CCC", "DDD"):
                s.add({"ticker": t, "company_name": t, "market": "us_stock", "tier": "focus"})
                s.save_snapshots([_snap(t, 10)])
            halt = {}
            res = {"AAA": "error: x", "BBB": "error: y", "CCC": "error: z", "DDD": "ok"}
            with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch",
                       AsyncMock(return_value=res)):
                evts = [e async for e in _ensure_price_freshness(s, "us_stock", halt)]
            assert halt["stop"] is True, halt
            assert any(e["event"] == "data_refresh_block" for e in evts)
            print("OK: majority-fail hard-stop verified")

    asyncio.run(_main())


@pytest.mark.asyncio
async def test_lagging_bar_date_gets_refreshed_without_hard_stop(store):
    """fetched_at 刚刷过、但最新 bar 落后于同批票 → 补刷落后票，且不因补刷失败硬停。

    生产 9-28：yfinance 限流、兜底源尚无当日 bar，「抓取成功」把 fetched_at 刷成刚才，
    47 票里 35 票的最新收盘停在 9-25，决策照常用旧价，而旧时效门只看 fetched_at 看不出来。
    """
    today = datetime.now(timezone.utc).date()
    for t, lag in (("AAA", 0), ("BBB", 0), ("CCC", 3), ("DDD", 3)):
        store.save_snapshots([{"ticker": t, "date": (today - timedelta(days=lag)).isoformat(),
                               "close": 100.0, "fetched_at": _iso(0), "market": "us_stock"}])
    halt = {}
    evts, fake = await _collect(store, "us_stock", halt, results={"CCC": "error: 429", "DDD": "error: 429"})
    assert sorted(fake.call_args.args[0]) == ["CCC", "DDD"]
    assert not halt.get("stop")
