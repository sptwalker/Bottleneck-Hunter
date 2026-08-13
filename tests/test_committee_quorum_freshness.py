"""P1-④ 投委会法定人数(A) + 上游新鲜度闸(B) + context 每标的重建(C)。

- (A) `_fallback_consensus`：有效(approve/reject)委员 < QUORUM_MIN → final_verdict=needs_review，
      堵「1 人 approve+余皆故障→approved」与「全故障 decisive=0→假 rejected」两类误判。
- (C) `run_committee_review`：某标的背景聚合抛异常时，该标的 context 显式标注「背景缺失」，
      不残留上一标的的估值/情绪（每 plan 从市场级基底浅拷贝重建）。
- (B) `_upstream_age_days` + `run_tactical_plans`：strategic/macro 超周度阈值 → 阻断，不据陈旧上游产今日战术。
"""
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist import committee as C
from bottleneck_hunter.watchlist.committee import QUORUM_MIN, _fallback_consensus
from bottleneck_hunter.watchlist.decision_engine import (
    _STALE_UPSTREAM_DAYS,
    _upstream_age_days,
    run_tactical_plans,
)
from bottleneck_hunter.watchlist.store import WatchlistStore


def _store(tmp_path, name="t.db"):
    return WatchlistStore(str(tmp_path / name)).for_user("u1").for_market("us_stock")


# ────────────────────────── (A) 法定人数 ──────────────────────────

def test_quorum_one_approve_rest_error_is_needs_review():
    """1 人 approve + 3 人故障弃权 → 有效票=1 < QUORUM_MIN → needs_review（旧逻辑会 approved）。"""
    reviews = {
        "value": {"vote": "approve", "confidence": 7},
        "growth": {"vote": "abstain", "error": "LLM 超时"},
        "risk": {"vote": "abstain", "error": "LLM 超时"},
        "contrarian": {"vote": "abstain", "error": "LLM 超时"},
    }
    assert _fallback_consensus(reviews)["final_verdict"] == "needs_review"


def test_quorum_all_error_not_false_rejected():
    """全员故障 → decisive=0，旧逻辑误判 rejected；法定人数闸下应 needs_review（人工复核）。"""
    reviews = {r: {"vote": "abstain", "error": "x"} for r in ("value", "growth", "risk", "contrarian")}
    assert _fallback_consensus(reviews)["final_verdict"] == "needs_review"


def test_quorum_met_two_valid_still_decides():
    """2 人 approve + 2 弃权 → 有效票=2≥QUORUM_MIN → 闸不触发，正常 approved（证明未过度拦截）。"""
    assert QUORUM_MIN == 2
    reviews = {
        "value": {"vote": "approve", "confidence": 7},
        "growth": {"vote": "approve", "confidence": 6},
        "risk": {"vote": "abstain", "error": "x"},
        "contrarian": {"vote": "abstain", "error": "x"},
    }
    assert _fallback_consensus(reviews)["final_verdict"] == "approved"


def test_quorum_counts_plural_vote_aliases():
    """LLM 返回复数/verdict 风格票值(approve_with_modifications/approved)须归一化后计入有效票，
    否则 2 个本应有效的赞成被误当弃权 → valid_n=0 → 假 needs_review（quorum 闸过度触发）。"""
    reviews = {
        "value": {"vote": "approve_with_modifications", "confidence": 7},  # 复数变体
        "growth": {"vote": "approved", "confidence": 6},                   # verdict 风格
        "risk": {"vote": "abstain", "error": "x"},
        "contrarian": {"vote": "abstain", "error": "x"},
    }
    assert _fallback_consensus(reviews)["final_verdict"] == "approved"


# ────────────────────────── (C) context 每标的重建 ──────────────────────────

async def test_ticker_background_failure_does_not_bleed(tmp_path, monkeypatch):
    """AAA 背景成功→BBB 背景抛异常：BBB 的 context 必须标注「背景缺失」，绝不残留 AAA 的估值。"""
    store = _store(tmp_path)

    def fake_bg(store_, ticker, entry_id, market):
        if ticker == "BBB":
            raise RuntimeError("模拟背景聚合失败")
        return {"valuation_data": {"trailing_pe": "42.0"}, "sentiment_data": "看多",
                "catalyst_data": [], "crowding_data": "低", "peer_comparison": "略",
                "sector_trends": "上行"}

    captured: dict[str, dict] = {}

    async def fake_review(member, execution_plan, context):
        captured[execution_plan.get("ticker", "?")] = dict(context)  # 快照该标的实际收到的背景
        return {"role": member["role"], "vote": "abstain", "confidence": 5}

    async def fake_consensus(reviews, discussion, weights):
        return {"final_verdict": "needs_review", "summary": "", "consensus_modifications": []}

    monkeypatch.setattr(C, "build_ticker_background", fake_bg)
    monkeypatch.setattr(C, "_review_single", fake_review)
    monkeypatch.setattr(C, "_build_consensus", fake_consensus)

    pending = [
        {"id": "p_aaa", "ticker": "AAA", "entry_id": "", "result_json": {"ticker": "AAA", "action": "buy"}},
        {"id": "p_bbb", "ticker": "BBB", "entry_id": "", "result_json": {"ticker": "BBB", "action": "buy"}},
    ]
    async for _ in C.run_committee_review(store, pending, budget=None, market="us_stock"):
        pass

    assert captured["AAA"]["valuation_data"] == {"trailing_pe": "42.0"}
    # 关键：BBB 背景失败 → 显式缺失标注，而非沿用 AAA 的估值
    assert "背景聚合失败" in str(captured["BBB"]["valuation_data"])
    assert captured["BBB"]["valuation_data"] != captured["AAA"]["valuation_data"]
    assert "背景聚合失败" in str(captured["BBB"]["sentiment_data"])


# ────────────────────────── (B) 上游新鲜度闸 ──────────────────────────

def test_upstream_age_days():
    now = datetime.now(timezone.utc)
    assert _upstream_age_days(now.isoformat(timespec="seconds")) < 0.02
    stale = (now - timedelta(days=_STALE_UPSTREAM_DAYS + 2)).isoformat(timespec="seconds")
    assert _upstream_age_days(stale) > _STALE_UPSTREAM_DAYS
    assert _upstream_age_days("") is None        # 空 → 视作陈旧
    assert _upstream_age_days("not-a-date") is None


async def test_l3_blocks_on_stale_upstream(tmp_path):
    """真实数据：strategic 回填成 10 天前 → run_tactical_plans 在取 LLM 前阻断，不产今日战术。"""
    store = _store(tmp_path, "stale.db")
    macro_id = store.create_macro_strategy({"market_summary": "中性", "stance": "neutral"})
    store.create_strategic_plan(macro_id, {"overall_stance": "neutral", "stock_selection": {}})

    # 新鲜时：不应因「未刷新」阻断（无 LLM 会另报错，但不该是新鲜度错）
    fresh_events = [evt async for evt in run_tactical_plans(store, market="us_stock")]
    fresh_errs = [e for e in fresh_events if e.get("event") == "decision_error"]
    assert not any("未刷新" in e["data"].get("error", "") for e in fresh_errs)

    # 回填 strategic.created_at 到 10 天前 → 应阻断
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    with store._write_conn() as conn:
        conn.execute("UPDATE strategic_plans SET created_at = ?", (old,))
    events = [evt async for evt in run_tactical_plans(store, market="us_stock")]
    errs = [e for e in events if e.get("event") == "decision_error"]
    assert any("未刷新" in e["data"].get("error", "") and "L2" in e["data"].get("error", "") for e in errs), \
        f"陈旧上游未被阻断: {[e['data'].get('error') for e in errs]}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
