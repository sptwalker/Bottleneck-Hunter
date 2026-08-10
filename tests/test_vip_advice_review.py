"""Phase 5b · VIP 复盘闭环存储/逻辑回归：
- C-2 list_projections 区间过滤（since_date/until_date）
- C-3 record_outcome 按 prediction_date 逐条结算 + list_pending_predictions + sim(vote) 隔离
- C-3 review_pending_advice 端到端（造行情快照 → 再评即结）
- C-4 run_attribution 轻量归因（结算单 diff → 写推断·非确认日志）
"""
import pytest

from bottleneck_hunter.vip.advice_review import review_pending_advice
from bottleneck_hunter.vip.advisory import VIP_PT_ADVICE, VIP_ROLE_CONTEXT
from bottleneck_hunter.vip.attribution import run_attribution
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path):
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def test_list_projections_date_range(wl):
    for d in ("2026-05-01", "2026-06-01", "2026-07-01"):
        wl.upsert_projection(account_ref="A", as_of_date=d, ticker="NVDA")
    assert len(wl.list_projections(account_ref="A")) == 3
    assert len(wl.list_projections(account_ref="A", since_date="2026-06-01")) == 2
    assert len(wl.list_projections(account_ref="A", until_date="2026-06-01")) == 2
    mid = wl.list_projections(account_ref="A", since_date="2026-06-01", until_date="2026-06-30")
    assert len(mid) == 1 and mid[0]["as_of_date"] == "2026-06-01"


def test_record_outcome_settles_only_matching_date(wl):
    # 同标的、同 prediction_type、两条不同预测日的 pending（模拟不同周期各打一次点）
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="NVDA", prediction_type=VIP_PT_ADVICE, prediction_value="加仓")
    # 手改一条的 prediction_date 到更早，制造双周期
    conn = wl._connect()
    rows = conn.execute("SELECT id FROM model_accuracy ORDER BY rowid").fetchall()
    conn.close()
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="NVDA", prediction_type=VIP_PT_ADVICE, prediction_value="加仓")
    with wl._write_conn() as c:
        c.execute("UPDATE model_accuracy SET prediction_date='2026-05-01' WHERE id=?", (rows[0]["id"],))
    pend = wl.list_pending_predictions(role_context=VIP_ROLE_CONTEXT, prediction_types=[VIP_PT_ADVICE])
    assert len(pend) == 2
    # 只结早的那一条
    rc = wl.record_outcome("NVDA", VIP_PT_ADVICE, outcome_value="x",
                           score_delta=0.0, prediction_date="2026-05-01")
    assert rc == 1
    still = wl.list_pending_predictions(role_context=VIP_ROLE_CONTEXT, prediction_types=[VIP_PT_ADVICE])
    assert len(still) == 1  # 另一日仍 pending，未被误结


def test_review_isolates_sim_vote(wl):
    # sim 的 vote 行绝不被 VIP 复盘误结
    wl.record_prediction(provider="p", model="m", role_context="committee_bull",
                         ticker="NVDA", prediction_type="vote", prediction_value="buy")
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="NVDA", prediction_type=VIP_PT_ADVICE, prediction_value="加仓")
    pdate = wl.list_pending_predictions(role_context=VIP_ROLE_CONTEXT)[0]["prediction_date"]
    # 造行情：预测日 100 → 第 5 个交易日 110（+10% → 加仓判对）。固定持有期需 ≥6 行凑满 5 日。
    snaps = [{"ticker": "NVDA", "date": pdate, "close": 100.0, "market": "us_stock"}]
    snaps += [{"ticker": "NVDA", "date": f"2099-01-{d:02d}", "close": 100.0 + 2 * d, "market": "us_stock"}
              for d in range(1, 6)]  # 第5个交易日(2099-01-05) close=110
    wl.save_snapshots(snaps)
    stats = review_pending_advice(wl)
    assert stats["reviewed"] == 1 and stats["correct"] == 1
    # sim 的 vote 仍是 pending（is_correct=-1）
    vote = wl.list_pending_predictions(prediction_types=["vote"])
    assert len(vote) == 1


def test_review_skips_no_price(wl):
    wl.record_prediction(provider="p", model="m", role_context=VIP_ROLE_CONTEXT,
                         ticker="0700.HK", prediction_type=VIP_PT_ADVICE, prediction_value="减仓")
    stats = review_pending_advice(wl)
    assert stats["no_price"] == 1 and stats["reviewed"] == 0  # 无快照 → 跳过不硬结


def test_attribution_writes_inferred_log(wl):
    # 结算单 diff：NVDA 平仓、AMD 大幅减仓 → 两条「归因·…」日志，标注推断·非确认
    old = [{"ticker": "NVDA", "shares": 100, "market_value": 10000.0},
           {"ticker": "AMD", "shares": 50, "market_value": 5000.0}]
    new = [{"symbol": "AMD", "quantity": 30, "market_value_base": 3000.0}]  # NVDA 缺席=平仓
    stats = run_attribution(wl, "A", old, new)
    assert stats["events"] == 2 and stats["closed"] == 1 and stats["changed"] == 1
    assert stats["escalated"] is False  # 开关默认关
    logs = wl.list_account_log(account_ref="A", event_type="calibration")
    assert len(logs) == 2 and all("推断·非确认" in r["detail"] for r in logs)


def test_attribution_market_isolated(wl):
    # 归因日志按 for_market 隔离：A股账户看不到美股账户的归因
    run_attribution(wl, "A", [{"ticker": "NVDA", "shares": 100, "market_value": 10000.0}],
                    [{"symbol": "NVDA", "quantity": 0, "market_value_base": 0.0}])
    cn = wl.for_market("cn_stock")
    assert len(cn.list_account_log(account_ref="A", event_type="calibration")) == 0
