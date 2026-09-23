"""P1-2：质量门红灯导致「缺口未纠正」时，必须在 operation_log 留一条痕。

阻断买是对的（数据过期/仓位超限不能盲下单），但那笔"该配没配"也得像"被拦的买"一样记账，
否则事后只看到"买了的被拦"，看不到"没买的欠着"。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
from bottleneck_hunter.watchlist.store import WatchlistStore

UID = "u_gap"


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(db_path=tmp_path / "t.db", user_id=UID).for_user(UID).for_market("us_stock")
    s.add({"ticker": "AAPL", "company_name": "Apple", "tier": "focus", "market": "us_stock"})
    # L1 判激进多头 → 权益下限 60%；账户却 10% 权益/90% 现金 → 缺口 50pct
    s.create_macro_strategy(
        {"regime": "bull", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False
    )
    acct = s.get_sim_account()
    s.create_sim_position(acct["id"], "AAPL", shares=1000, avg_cost=10.0)
    s.update_sim_position(s.get_sim_position(acct["id"], "AAPL")["id"], current_price=10.0, market_value=10_000.0)
    s.update_sim_account(cash_balance=90_000.0, total_equity=100_000.0, current_capital=100_000.0)
    return s


async def _collect(gen):
    return [e async for e in gen]


async def _noop_gen(*_a, **_k):
    return
    yield  # pragma: no cover —— 空异步生成器


def _red_gate(*_a, **_k):
    async def _g():
        yield {"event": "quality_check_block", "data": {"event": "quality_check_block",
                                                        "stage": "pre_l4", "severity": "red",
                                                        "reason": "数据过期"}}
    return _g()


def test_缺口未纠正留痕(store):
    with (
        patch("bottleneck_hunter.watchlist.decision_engine._ensure_price_freshness", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine._hard_stop_loss_sweep", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_tactical_plans", _noop_gen),
        patch("bottleneck_hunter.watchlist.quality_gate.run_quality_checks", _red_gate),
    ):
        # 留痕走 web.oplog.record_operation → 需注入 store 才能落库
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        events = asyncio.run(_collect(run_daily_decision(store, scope="l3l4")))

    assert any(e["event"] == "quality_check_block" for e in events), "前提：质量门须真判红"
    ops = store.get_operations(UID, category="error")
    titles = [o["title"] for o in ops]
    assert "质量门阻断 L4" in titles
    gap = [o for o in ops if o["title"] == "缺口未纠正"]
    assert gap, f"缺口未纠正必须留痕，实际只有 {titles}"
    detail = gap[0]["detail"]
    assert "权益配置不足" in detail and "下限 60" in detail and "缺口 50" in detail, detail
    assert gap[0]["market"] == "us_stock" and gap[0]["result"] == "partial"


def test_无缺口不留痕(store):
    """权益已在下限之上（无缺口）时，红灯只记「质量门阻断」，不该凭空多一条缺口记录。"""
    acct = store.get_sim_account()
    store.update_sim_position(store.get_sim_position(acct["id"], "AAPL")["id"],
                              current_price=70.0, market_value=70_000.0)
    store.update_sim_account(cash_balance=30_000.0, total_equity=100_000.0, current_capital=100_000.0)
    with (
        patch("bottleneck_hunter.watchlist.decision_engine._ensure_price_freshness", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine._hard_stop_loss_sweep", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_tactical_plans", _noop_gen),
        patch("bottleneck_hunter.watchlist.quality_gate.run_quality_checks", _red_gate),
    ):
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        asyncio.run(_collect(run_daily_decision(store, scope="l3l4")))

    titles = [o["title"] for o in store.get_operations(UID, category="error")]
    assert "质量门阻断 L4" in titles
    assert "缺口未纠正" not in titles


def test_上游陈旧也留痕(store):
    """L2 已 10 天未刷新（L3 因陈旧中止）+ 权益缺口 → 质量门没红也得记「缺口未纠正」，原因写明陈旧。"""
    from datetime import datetime, timedelta, timezone

    mid = store.get_latest_macro_strategy()["id"]
    store.create_strategic_plan(mid, {"target_allocation": {"equity_pct": 60, "cash_pct": 40}}, strict=False)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    with store._write_conn() as conn:
        conn.execute("UPDATE strategic_plans SET created_at = ?", (old,))

    def _green_gate(*_a, **_k):
        async def _g():
            return
            yield  # pragma: no cover
        return _g()

    with (
        patch("bottleneck_hunter.watchlist.decision_engine._ensure_price_freshness", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine._hard_stop_loss_sweep", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_tactical_plans", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_execution_plans", _noop_gen),
        patch("bottleneck_hunter.watchlist.quality_gate.run_quality_checks", _green_gate),
    ):
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        asyncio.run(_collect(run_daily_decision(store, scope="l3l4")))

    ops = store.get_operations(UID, category="error")
    assert "质量门阻断 L4" not in [o["title"] for o in ops], "前提：质量门未红"
    gap = [o for o in ops if o["title"] == "缺口未纠正"]
    assert gap, "上游陈旧导致停买时，缺口同样必须留痕"
    assert "L2 陈旧" in gap[0]["detail"] and "缺口 50" in gap[0]["detail"], gap[0]["detail"]
