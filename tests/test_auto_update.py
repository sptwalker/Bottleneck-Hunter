"""验收：用户可配置的数据自动更新方案。

覆盖：per-user 配置 + 分类门控 (_get_active_user_stores) + job_stale_refresh 只刷超阈值标的
+ 全局时间表读写 + confirm 后拉实时价重算持仓。
运行：pytest tests/test_auto_update.py -q
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist import scheduler as S
from bottleneck_hunter.watchlist import schedule_config as SC


@pytest.fixture
def tmp():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ── 配置存储 ────────────────────────────────────────────────
class TestConfig:
    def test_defaults_and_isolation(self, tmp):
        base = WatchlistStore(db_path=str(Path(tmp) / "s.db"))
        a, b = base.for_user("A"), base.for_user("B")
        assert a.get_auto_update_config()["master_enabled"] == "1"
        a.set_auto_update_config("daily_decision", "0")
        assert a.get_auto_update_config()["daily_decision"] == "0"
        assert b.get_auto_update_config()["daily_decision"] == "1"  # 隔离

    def test_master_gates_all(self, tmp):
        s = WatchlistStore(db_path=str(Path(tmp) / "s.db")).for_user("A")
        assert s.is_auto_update_enabled("catalyst") is True
        s.set_auto_update_config("master_enabled", "0")
        assert s.is_auto_update_enabled("catalyst") is False

    def test_unknown_key_rejected(self, tmp):
        s = WatchlistStore(db_path=str(Path(tmp) / "s.db")).for_user("A")
        s.set_auto_update_config("bogus", "1")
        assert "bogus" not in s.get_auto_update_config()


# ── 分类门控 ────────────────────────────────────────────────
class _FakeAuth:
    def __init__(self, uids, cfg_global="1"):
        self._uids = uids
        self._cfg = {SC.GLOBAL_ENABLED_KEY: cfg_global}

    def list_active_user_ids(self):
        return self._uids

    def get_config(self, key, default=""):
        return self._cfg.get(key, default)


class TestGating:
    def test_category_filters_users(self, tmp):
        base = WatchlistStore(db_path=str(Path(tmp) / "s.db"))
        base.for_user("A").set_auto_update_config("daily_decision", "0")  # A 关决策
        S._wl_store = base
        S._auth_store = _FakeAuth(["A", "B"])
        try:
            got = [uid for uid, _s, _b in S._get_active_user_stores("daily_decision")]
            assert got == ["B"]  # A 被跳过
            got2 = [uid for uid, _s, _b in S._get_active_user_stores("catalyst")]
            assert set(got2) == {"A", "B"}  # catalyst 都开
        finally:
            S._wl_store = None; S._auth_store = None

    def test_global_killswitch(self, tmp):
        base = WatchlistStore(db_path=str(Path(tmp) / "s.db"))
        S._wl_store = base
        S._auth_store = _FakeAuth(["A"], cfg_global="0")  # 全局关
        try:
            assert S._get_active_user_stores("daily_decision") == []
            # 无 category 时不门控
            assert len(S._get_active_user_stores()) == 1
        finally:
            S._wl_store = None; S._auth_store = None


# ── 陈旧刷新 ────────────────────────────────────────────────
class TestStaleRefresh:
    def test_only_refreshes_stale(self, tmp):
        base = WatchlistStore(db_path=str(Path(tmp) / "s.db"))
        S._wl_store = base
        S._auth_store = None  # 单用户
        S._budget = None
        calls = {"price": []}

        async def _fake_price(tickers, store, market="us_stock", **k):
            calls["price"].append((market, list(tickers)))
            return {t: "ok" for t in tickers}

        async def _noop(*a, **k):
            if False:
                yield  # make it an async generator
            return

        try:
            with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", _fake_price), \
                 patch("bottleneck_hunter.watchlist.strategy_engine.refresh_intelligence_one", _noop), \
                 patch("bottleneck_hunter.watchlist.strategy_engine.refresh_strategy_one", _noop), \
                 patch.object(base, "get_stale_tickers",
                              return_value=[{"ticker": "NVDA", "market": "us_stock"}]), \
                 patch.object(base, "get_by_ticker", return_value=None):
                asyncio.run(S.job_stale_refresh())
            assert calls["price"] == [("us_stock", ["NVDA"])]
        finally:
            S._wl_store = None; S._auth_store = None


# ── 全局时间表 ──────────────────────────────────────────────
class TestGlobalSchedule:
    def test_defaults(self):
        sch = SC.get_global_schedule(None)
        assert sch["us_daily_decision"]["hour"] == 6      # 在数据 job 之后
        assert sch["stale_refresh"]["interval_hours"] == 6

    def test_override_merge(self):
        class _A:
            def __init__(self): self.store = {}
            def get_config(self, k, d=""): return self.store.get(k, d)
            def set_config(self, k, v): self.store[k] = v
        a = _A()
        SC.set_global_schedule(a, {"us_daily_decision": {"hour": 20}})
        sch = SC.get_global_schedule(a)
        assert sch["us_daily_decision"]["hour"] == 20
        assert sch["us_daily_decision"]["minute"] == 30  # 未覆盖 → 保留默认(30)


# ── 操作后实时刷新 ─────────────────────────────────────────
class TestPostActionRefresh:
    def test_refresh_positions_live(self, tmp):
        from bottleneck_hunter.watchlist import trade_executor as TE
        base = WatchlistStore(db_path=str(Path(tmp) / "s.db")).for_market("us_stock")
        acct = base.get_sim_account()
        base.update_sim_account(cash_balance=1_000_000, initial_capital=1_000_000)
        # 造一个持仓
        base.create_sim_position(account_id=acct["id"], ticker="NVDA", shares=10,
                                 avg_cost=100.0, entry_id=None)

        async def _fake_price(tickers, store, market="us_stock", **k):
            # 模拟拉到新价 → 写快照
            store.save_snapshots([{"ticker": t, "date": "2026-07-03", "close": 130.0,
                                   "open": 130, "high": 130, "low": 130, "volume": 1, "market": market}
                                  for t in tickers])
            return {t: "ok" for t in tickers}

        with patch("bottleneck_hunter.watchlist.price_pipeline.fetch_price_batch", _fake_price):
            asyncio.run(TE.refresh_positions_live(base))

        pos = base.get_sim_position(acct["id"], "NVDA")
        # 持仓市值用新价 130 重算：10*130=1300，浮盈 (130-100)*10=300
        assert pos["current_price"] == 130.0
        assert pos["market_value"] == 1300.0
        assert pos["unrealized_pnl"] == 300.0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
