"""决策运行台账（2026-10）：每轮决策的输入/产出/参与模型 + T+5 结果回填。

目的：充值恢复多 provider 后，能长期跟踪「这轮建议一个周期后到底怎么样」。
"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(str(tmp_path / "runs.db")).for_user("u1").for_market("a_stock")


def test_create_and_read_run(store):
    rid = store.create_decision_run(
        market="a_stock", run_date="2026-10-29", trigger="daily",
        payload={"l1_id": "m1", "l2_id": "s1", "l2_equity_pct": 36,
                 "equity_pct_before": 3.85, "l3_count": 5, "l4_count": 3,
                 "committee_total": 3, "committee_approved": 2,
                 "provider_count": 3, "providers": "deepseek,glm,qwen",
                 "outcome_due_date": "2026-11-03",
                 "meta_json": {"regime": "transition"}},
    )
    run = store.get_decision_run(rid)
    assert run["run_date"] == "2026-10-29"
    assert run["equity_pct_before"] == 3.85
    assert run["l2_equity_pct"] == 36
    assert run["providers"] == "deepseek,glm,qwen"
    assert run["outcome_status"] == "pending"
    assert run["meta_json"]["regime"] == "transition"
    assert run["user_id"] == "u1" and run["market"] == "a_stock"


def test_list_and_filter(store):
    store.create_decision_run(market="a_stock", run_date="2026-10-29",
                              payload={"outcome_due_date": "2026-11-03"})
    store.create_decision_run(market="a_stock", run_date="2026-10-28",
                              payload={"outcome_due_date": "2026-11-02"})
    assert len(store.list_decision_runs(market="a_stock")) == 2
    assert [r["run_date"] for r in store.list_decision_runs(market="a_stock")] == ["2026-10-29", "2026-10-28"]
    assert len(store.list_decision_runs(market="a_stock", outcome_status="pending")) == 2


def test_pending_due_only(store):
    store.create_decision_run(market="a_stock", run_date="2026-10-20",
                              payload={"outcome_due_date": "2026-10-25"})
    store.create_decision_run(market="a_stock", run_date="2026-10-29",
                              payload={"outcome_due_date": "2026-11-03"})
    due = store.get_pending_decision_runs("2026-10-29")
    assert len(due) == 1 and due[0]["run_date"] == "2026-10-20"


def test_update_settles_run(store):
    rid = store.create_decision_run(market="a_stock", run_date="2026-10-20",
                                    payload={"equity_pct_before": 3.85,
                                             "outcome_due_date": "2026-10-25"})
    assert store.update_decision_run(rid, outcome_status="settled",
                                     equity_pct_after=8.2, equity_change_pct=4.35)
    run = store.get_decision_run(rid)
    assert run["outcome_status"] == "settled"
    assert run["equity_change_pct"] == 4.35
    assert store.get_pending_decision_runs("2026-10-29") == []


def test_settle_decision_runs_backfills(store):
    """到期记录按当前权益占比结算；未到期的保持 pending。"""
    from bottleneck_hunter.watchlist.decision_engine import settle_decision_runs

    acct = store.get_sim_account()
    store.create_sim_position(acct["id"], "002815.SZ", 1000, 10.0)  # 市值 1 万
    store.update_sim_account(cash_balance=90000, total_equity=100000, current_capital=100000)
    rid = store.create_decision_run(market="a_stock", run_date="2026-10-20",
                                    payload={"equity_pct_before": 1.0,
                                             "outcome_due_date": "2026-10-25"})
    fresh = store.create_decision_run(market="a_stock", run_date="2026-10-29",
                                      payload={"equity_pct_before": 1.0,
                                               "outcome_due_date": "2026-11-03"})

    n = settle_decision_runs(store, as_of="2026-10-29")
    assert n == 1
    run = store.get_decision_run(rid)
    assert run["outcome_status"] == "settled"
    assert run["equity_pct_after"] == 10.0        # 10000 / 100000
    assert run["equity_change_pct"] == 9.0
    assert store.get_decision_run(fresh)["outcome_status"] == "pending"


def test_record_decision_run_captures_context(store):
    from bottleneck_hunter.watchlist.decision_engine import record_decision_run

    store.create_strategic_plan("", {"target_allocation": {"equity_pct": 36},
                                     "stock_selection": {"core_holdings": [{"ticker": "002815.SZ",
                                                                            "target_weight_pct": 8.5}],
                                                         "tactical_holdings": []}}, strict=False)
    rid = record_decision_run(store, "a_stock", trigger="daily", l4_plan_count=2)
    run = store.get_decision_run(rid)
    assert run["trigger"] == "daily"
    assert run["l2_equity_pct"] == 36
    assert run["l4_count"] == 2
    assert run["outcome_status"] == "pending"
    assert len(run["outcome_due_date"]) == 10      # T+5 预约
    assert run["meta_json"]["target_weights"] == {"002815.SZ": 8.5}


def test_settle_computes_drift_toward_target(store):
    """有 L2 目标时，结算要给 drift：向目标收窄为负，远离为正。"""
    from bottleneck_hunter.watchlist.decision_engine import settle_decision_runs

    acct = store.get_sim_account()
    store.create_sim_position(acct["id"], "002815.SZ", 1000, 10.0)  # 市值 1 万 = 10%
    store.update_sim_account(cash_balance=90000, total_equity=100000, current_capital=100000)
    rid = store.create_decision_run(
        market="a_stock", run_date="2026-10-20",
        payload={"equity_pct_before": 5.0, "l2_equity_pct": 10.0, "outcome_due_date": "2026-10-25"},
    )

    settle_decision_runs(store, as_of="2026-10-29")
    run = store.get_decision_run(rid)
    # 决策时距目标 |5-10|=5，结算时距目标 |10-10|=0 → 偏差 = 0-5 = -5（向目标收窄）
    assert run["drift_pct"] == -5.0


def test_settle_drift_positive_when_moving_away(store):
    """反向扩张：离目标更远 → drift 为正。"""
    from bottleneck_hunter.watchlist.decision_engine import settle_decision_runs

    acct = store.get_sim_account()
    store.create_sim_position(acct["id"], "002815.SZ", 3000, 10.0)  # 3 万 = 30%
    store.update_sim_account(cash_balance=70000, total_equity=100000, current_capital=100000)
    rid = store.create_decision_run(
        market="a_stock", run_date="2026-10-20",
        payload={"equity_pct_before": 10.0, "l2_equity_pct": 10.0, "outcome_due_date": "2026-10-25"},
    )

    settle_decision_runs(store, as_of="2026-10-29")
    # 决策时距目标 0，结算时 |30-10|=20 → 偏差 +20
    assert store.get_decision_run(rid)["drift_pct"] == 20.0


def test_drift_is_none_without_target(store):
    """无 L2 目标时 drift 必须为 None，不能填 0——否则"没数据"与"零偏差"混为一谈。"""
    from bottleneck_hunter.watchlist.decision_engine import settle_decision_runs

    acct = store.get_sim_account()
    store.create_sim_position(acct["id"], "002815.SZ", 1000, 10.0)
    store.update_sim_account(cash_balance=90000, total_equity=100000, current_capital=100000)
    rid = store.create_decision_run(
        market="a_stock", run_date="2026-10-20",
        payload={"equity_pct_before": 1.0, "outcome_due_date": "2026-10-25"},
    )

    settle_decision_runs(store, as_of="2026-10-29")
    assert store.get_decision_run(rid)["drift_pct"] is None


def test_equity_drift_helper_edges():
    """纯函数边界：缺任一输入都返回 None，坏值不抛。"""
    from bottleneck_hunter.watchlist.decision_engine import _equity_drift

    assert _equity_drift({"l2_equity_pct": None, "equity_pct_before": 5}, 10) is None
    assert _equity_drift({"l2_equity_pct": 10, "equity_pct_before": None}, 10) is None
    assert _equity_drift({"l2_equity_pct": 10, "equity_pct_before": 5}, None) is None
    assert _equity_drift({"l2_equity_pct": "bad", "equity_pct_before": 5}, 10) is None
