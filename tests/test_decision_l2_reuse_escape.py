"""P1-1：L2 漂移逃逸阀——组合已严重偏离旧 L2 目标时，日常流程不再只做偏离检查，强制重生成 L2。"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore

TARGET = {"target_allocation": {"equity_pct": 60, "cash_pct": 40}}


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(db_path=tmp_path / "t.db").for_user("test").for_market("us_stock")


def _setup(store, cash_pct: float, plan_rj: dict):
    """建一个 10 万账户，按 cash_pct 配现金/持仓；plan_rj 为目标图纸。"""
    s = store.for_market("us_stock")
    acct = s.get_sim_account()
    total = 100_000.0
    cash = total * cash_pct / 100
    pos_value = total - cash
    if pos_value > 0:
        eid = s.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus", "market": "us_stock"})
        pid = s.create_sim_position(acct["id"], "NVDA", shares=int(pos_value / 100), avg_cost=100.0, entry_id=eid)
        s.update_sim_position(pid, current_price=100.0, market_value=pos_value)
    s.update_sim_account(cash_balance=cash, total_equity=total, current_capital=total)
    return s


def test_drift_escapes_reuse(store):
    """现金 90% vs 目标 40% → 偏离 50pct > 15pct 阈值 → 逃逸。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup(store, cash_pct=90.0, plan_rj={})
    esc = _reuse_escape_reason(s, TARGET, "us_stock")
    assert esc, "现金/权益严重偏离时必须逃逸"
    assert esc["reason"]
    assert abs(esc["drift_pct"]) == pytest.approx(50.0, abs=0.2)


def test_small_drift_reuses(store):
    """实际与目标仅差几个点 → 不逃逸（不烧算力）。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup(store, cash_pct=45.0, plan_rj={})
    assert _reuse_escape_reason(s, TARGET, "us_stock") == {}


def test_no_target_never_escapes(store):
    """旧格式/缺 target_allocation → 无从判偏离，降级为不逃逸。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup(store, cash_pct=90.0, plan_rj={})
    assert _reuse_escape_reason(s, {"overall_stance": "balanced"}, "us_stock") == {}


def test_all_cash_escapes(store):
    """纯现金账户 vs 60% 权益目标＝"钱没配出去"的最坏情形，必须逃逸（冷却另由日常流程控制）。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup(store, cash_pct=100.0, plan_rj={})
    esc = _reuse_escape_reason(s, TARGET, "us_stock")
    assert esc and abs(esc["drift_pct"]) == pytest.approx(60.0, abs=0.2)


def test_threshold_env_override(store, monkeypatch):
    """阈值可调：默认 15pct 不逃逸的偏离，阈值降到 3pct 后逃逸。"""
    import bottleneck_hunter.watchlist.decision_engine as de

    s = _setup(store, cash_pct=50.0, plan_rj={})
    assert de._reuse_escape_reason(s, TARGET, "us_stock") == {}
    monkeypatch.setattr(de, "_DECISION_DRIFT_ESCAPE_PCT", 3.0)
    assert de._reuse_escape_reason(s, TARGET, "us_stock")


def test_deposit_moves_total_equity(store):
    """入金/出金须同步平移 total_equity：偏离度与缺口都以它为分母，不同步则入金后权益%虚高、逃逸误判。"""
    from bottleneck_hunter.watchlist.decision_engine import _reuse_escape_reason

    s = _setup(store, cash_pct=40.0, plan_rj={})  # 60/40 正在目标上
    s.adjust_sim_funds("deposit", 200_000.0)
    assert s.get_sim_account()["total_equity"] == pytest.approx(300_000.0)
    esc = _reuse_escape_reason(s, TARGET, "us_stock")  # 真实现金 240k/300k = 80% → 偏离 +40
    assert esc and esc["drift"]["actual_cash_pct"] == pytest.approx(80.0, abs=0.1)
    s.adjust_sim_funds("withdraw", 200_000.0)
    assert s.get_sim_account()["total_equity"] == pytest.approx(100_000.0)


# ── 日常流程接线：逃逸必须真的在 run_daily_decision 里生效（此前只测 helper，接线是死的） ──

def _run_daily_step2(s, plan_age_h: float):
    """建 L1+L2（L2 回填到 plan_age_h 小时前），跑 scope=full，记录 Step 2 走了重生成还是偏离检查。"""
    from bottleneck_hunter.watchlist import decision_engine as de

    mid = s.create_macro_strategy({"regime": "sideways", "risk_appetite": "balanced"}, strict=False)
    s.create_strategic_plan(mid, TARGET, strict=False)
    old = (datetime.now(timezone.utc) - timedelta(hours=plan_age_h)).isoformat(timespec="seconds")
    with s._write_conn() as conn:
        conn.execute("UPDATE strategic_plans SET created_at = ?", (old,))

    calls: list[tuple] = []

    def _rec(name):
        async def _g(*_a, **k):
            calls.append((name, k.get("force", False)))
            return
            yield  # pragma: no cover
        return _g

    async def _noop(*_a, **_k):
        return
        yield  # pragma: no cover

    with (
        patch.object(de, "_ensure_price_freshness", _noop),
        patch.object(de, "_hard_stop_loss_sweep", _noop),
        patch.object(de, "run_macro_check", _noop),
        patch.object(de, "run_tactical_plans", _noop),
        patch.object(de, "run_execution_plans", _noop),
        patch.object(de, "run_strategic_plan", _rec("regen")),
        patch.object(de, "run_deviation_check", _rec("deviation")),
    ):
        events = asyncio.run(_collect(de.run_daily_decision(s, scope="full")))
    return calls, events


async def _collect(gen):
    return [e async for e in gen]


def test_daily_escapes_to_forced_regen(store):
    """L2 已 30h（冷却已过）+ 现金 90% vs 目标 40% → 走 force=True 重生成，不做偏离检查，且发出告警。"""
    s = _setup(store, cash_pct=90.0, plan_rj={})
    calls, events = _run_daily_step2(s, plan_age_h=30)
    assert calls == [("regen", True)], calls
    assert any(e["event"] == "decision_warning" and "偏离该 L2 目标" in e["data"].get("message", "")
               for e in events)


def test_daily_escape_cooldown(store):
    """L2 才 2h（本轮刚重生成过）→ 偏离照旧也不再重生成，回到偏离检查，避免每轮烧一次 L2。"""
    s = _setup(store, cash_pct=90.0, plan_rj={})
    calls, _ = _run_daily_step2(s, plan_age_h=2)
    assert calls == [("deviation", False)], calls


def test_daily_small_drift_keeps_deviation_check(store):
    """偏离在阈值内 → 照旧只做偏离检查。"""
    s = _setup(store, cash_pct=45.0, plan_rj={})
    calls, _ = _run_daily_step2(s, plan_age_h=30)
    assert calls == [("deviation", False)], calls
