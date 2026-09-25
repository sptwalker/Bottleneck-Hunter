"""P1-C + P1-D：缺口未纠正留痕的两条调用路径 + 现金超配侧。

P1-C：判据此前只长在 run_daily_decision 里，「一键全量刷新」这条路径看不到缺口 —— 抽成
共享 helper 后两条路径必须都有痕迹，且**不能**把 run_daily_decision 的措辞原样搬过来
（run_full_refresh 刚强制重生成 L1/L2，在那里「上游陈旧」是条死枝）。
P1-D：cash_max 此前算出来没人读（死字段），现金超配要在同一处记账，与权益下限对称。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap
from bottleneck_hunter.watchlist.decision_engine import (
    _l4_plan_count,
    _uncorrected_gap_cause,
    run_full_refresh,
)
from bottleneck_hunter.watchlist.store import WatchlistStore

UID = "u_gap2"
# sideways/balanced：equity(40,60) / cash(25,40) —— 40+40=80，两侧不是互补数
BOUNDS = {"equity_min": 40, "equity_max": 60, "cash_max": 40}


def _gap(equity_pct: float, bounds=BOUNDS):
    total = 100_000.0
    return compute_underweight_gap(
        {"total_equity": total, "cash_balance": (100 - equity_pct) / 100 * total},
        [{"ticker": "AAPL", "market_value": equity_pct / 100 * total}],
        bounds,
    )


class TestCashOverweightSide:
    def test_现金超上限即置位(self):
        """权益 10% / 现金 90%：两侧同时成立（权益低于 40 下限、现金高于 40 上限）。"""
        g = _gap(10.0)
        assert g["underweight"] is True and g["overweight_cash"] is True
        assert g["cash_pct"] == 90.0 and g["cash_max"] == 40
        assert g["excess_cash_pct"] == 50.0
        assert g["idle_cash"] == 50_000.0

    def test_两侧互不蕴含(self):
        """权益 50%（区间内，不低配）/ 现金 50%（超上限）→ 只触发现金侧。

        这正是现金上限不能由权益下限代劳的实证：equity(40,60) 与 cash(25,40) 不互补。
        """
        g = _gap(50.0)
        assert g["underweight"] is False
        assert g["overweight_cash"] is True and g["excess_cash_pct"] == 10.0

    def test_不超配不置位(self):
        """权益 70% / 现金 30%（在上限内）→ 两侧都不成立，缺口留痕不该凭空出现。"""
        g = _gap(70.0)
        assert g["underweight"] is False and g["overweight_cash"] is False
        assert g["idle_cash"] == 0.0 and g["excess_cash_pct"] == 0.0

    def test_闲置金额不超过手头现金(self):
        """封顶口径与 deployable_cash 一致：闲置金额是名义值，不得大于账户真有的现金。"""
        g = _gap(10.0)
        assert g["idle_cash"] <= 90_000.0

    def test_无bounds不误报(self):
        g = compute_underweight_gap({"total_equity": 100_000, "cash_balance": 90_000}, [], {})
        assert g["overweight_cash"] is False and g["idle_cash"] == 0.0


class TestL4PlanCountReader:
    """`plan_count` 字段存在性区分「跑了但零产出」与「全持有/没跑到」。"""

    def test_零产出取到0(self):
        assert _l4_plan_count({"event": "decision_done", "data": {"plan_count": 0}}, None) == 0

    def test_全持有不带字段则保持None(self):
        evt = {"event": "decision_done", "data": {"message": "L3 计划全部为持有，无需生成执行方案"}}
        assert _l4_plan_count(evt, None) is None

    def test_其它事件不改写已取值(self):
        assert _l4_plan_count({"event": "decision_start", "data": {}}, 3) == 3


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(db_path=tmp_path / "gap2.db", user_id=UID).for_user(UID).for_market("us_stock")
    s.add({"ticker": "AAPL", "company_name": "Apple", "tier": "focus", "market": "us_stock"})
    s.create_macro_strategy(
        {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False
    )
    acct = s.get_sim_account()
    # 10% 权益 / 90% 现金：两侧同时成立
    s.create_sim_position(acct["id"], "AAPL", shares=1000, avg_cost=10.0)
    s.update_sim_position(s.get_sim_position(acct["id"], "AAPL")["id"], current_price=10.0, market_value=10_000.0)
    s.update_sim_account(cash_balance=90_000.0, total_equity=100_000.0, current_capital=100_000.0)
    return s


async def _collect(gen):
    return [e async for e in gen]


async def _noop_gen(*_a, **_k):
    return
    yield  # pragma: no cover —— 空异步生成器


async def _noop_await(*_a, **_k):
    return 0


def _l4_zero_output(*_a, **_k):
    """L4 真跑到产出那一步但一条方案都没有（带 plan_count=0 的确证事件）。"""

    async def _g():
        yield {"event": "decision_done", "data": {"event": "decision_done", "layer": "L4", "plan_count": 0}}

    return _g()


def _run_refresh(store, l4):
    with (
        patch("bottleneck_hunter.watchlist.decision_engine._ensure_price_freshness", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_macro_strategy", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_strategic_plan", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_tactical_plans", _noop_gen),
        patch("bottleneck_hunter.watchlist.decision_engine.run_execution_plans", l4),
        patch("bottleneck_hunter.watchlist.news_pipeline.refresh_market_news", _noop_await),
    ):
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        events = asyncio.run(_collect(run_full_refresh(store)))
    assert any(e["event"] == "refresh_done" for e in events)
    return [o for o in store.get_operations(UID, category="error") if o["title"] == "缺口未纠正"]


def test_full_refresh也留痕且措辞诚实(store):
    """P1-C 的验收点：run_full_refresh 这条路现在也有痕迹，且原因是**诚实的**。

    本路径刚强制重生成 L1/L2，故「上游陈旧」在这里永不成立（不是漏检，是死枝）；
    L4 确证零产出（带 plan_count=0）才是这条路上真正能说出口的原因。
    """
    gap = _run_refresh(store, _l4_zero_output)

    assert gap, "全量刷新路径必须留下缺口痕迹（此前这条路上第一轮诊断的不对称原样保留）"
    detail = gap[0]["detail"]
    assert "陈旧" not in detail, "本路径刚重生成上游，不该套用陈旧措辞"
    assert "L4 未产出任何新方案" in detail, detail
    # P1-D：现金侧必须在同一条痕迹里出现（这是 cash_max 的第一个真实消费方）
    assert "现金超配" in detail and "上限 40" in detail, detail
    assert json.loads(gap[0]["meta_json"])["excess_cash_pct"] == 50.0


def test_没跑到产出那一步就不猜(store):
    """L4 事件流里连 decision_done 都没有（＝没跑到产出那一步）→ 不留痕。

    此时"本轮扩张侧是否停摆"根本无从判定，编一条出来就是假告警；宁可不记。
    """
    assert _run_refresh(store, _noop_gen) == []


def test_零产出措辞(store):
    """`plan_count==0` 才叫零产出；None（没跑到）与真跑完有产出都不留痕。"""
    assert _uncorrected_gap_cause(store, l4_plan_count=0) == "L4 未产出任何新方案"
    assert _uncorrected_gap_cause(store, l4_plan_count=3) == ""
    assert _uncorrected_gap_cause(store) == "", "没跑到产出那一步不并为一谈"


def test_两条路径共用同一判据(store):
    """同一账户状态下，两条路径对「该配没配」的判定必须一致（抽 helper 的全部意义）。"""
    mid = store.get_latest_macro_strategy()["id"]
    store.create_strategic_plan(mid, {"target_allocation": {"equity_pct": 60, "cash_pct": 40}}, strict=False)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    with store._write_conn() as conn:
        conn.execute("UPDATE strategic_plans SET created_at = ?", (old,))

    daily = _uncorrected_gap_cause(store)
    refresh = _uncorrected_gap_cause(store, l4_plan_count=0)
    assert daily == "上游 L2 陈旧（L3 已中止）"
    assert refresh == "上游 L2 陈旧（L3 已中止）", "上游真陈旧时，全量刷新路径同样应识别出来"
    # 陈旧优先于红灯/零产出：先说明"根本没资格谈",再说别的都是次要
    assert _uncorrected_gap_cause(store, l4_blocked=True, l4_plan_count=0) == daily


def test_正常运行不留痕(store):
    """L4 有产出、上游新鲜 → cause 空，不记噪音（现金超配本身可能是策略选择，不是事故）。"""
    assert _uncorrected_gap_cause(store, l4_plan_count=3) == ""
    assert store.get_operations(UID, category="error") == []
