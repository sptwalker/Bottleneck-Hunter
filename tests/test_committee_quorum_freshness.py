"""P1-④ 投委会法定人数(A) + 上游新鲜度闸(B) + context 每标的重建(C)。

- (A) `_fallback_consensus`：有效(approve/reject)委员 < QUORUM_MIN → final_verdict=needs_review，
      堵「1 人 approve+余皆故障→approved」与「全故障 decisive=0→假 rejected」两类误判。
- (C) `run_committee_review`：某标的背景聚合抛异常时，该标的 context 显式标注「背景缺失」，
      不残留上一标的的估值/情绪（每 plan 从市场级基底浅拷贝重建）。
- (B) `_upstream_age_days` + `run_tactical_plans`：strategic/macro 超周度阈值 → 阻断，不据陈旧上游产今日战术。
"""
from datetime import datetime, timedelta, timezone

import pytest

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

    from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
    bind = save_stage_snapshot(store, "L4", {"batch": ["AAA", "BBB"]})  # 同批共享快照，满足投委会父绑定闸
    pending = [
        {"id": "p_aaa", "ticker": "AAA", "entry_id": "",
         "snapshot_id": bind["snapshot_id"], "strategy_version": bind["strategy_version"],
         "result_json": {"ticker": "AAA", "action": "buy"}},
        {"id": "p_bbb", "ticker": "BBB", "entry_id": "",
         "snapshot_id": bind["snapshot_id"], "strategy_version": bind["strategy_version"],
         "result_json": {"ticker": "BBB", "action": "buy"}},
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
    macro_id = store.create_macro_strategy({"market_summary": "中性", "stance": "neutral"}, strict=False)
    store.create_strategic_plan(macro_id, {"overall_stance": "neutral", "stock_selection": {}}, strict=False)

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


# ────────────────────────── (D) P0-A：非结论性裁决必须落动作 ──────────────────────────

async def _run_committee_with_verdict(store, tmp_path, verdict, monkeypatch):
    """跑一遍 run_committee_review，投委会裁决固定为 verdict，返回执行计划终态。"""
    async def fake_review(member, execution_plan, context):
        return {"role": member["role"], "vote": "approve", "confidence": 6}

    async def fake_consensus(reviews, discussion, weights):
        return {"final_verdict": verdict, "summary": "测试结论",
                "consensus_modifications": [], "approval_rate": 0.5}

    monkeypatch.setattr(C, "_review_single", fake_review)
    monkeypatch.setattr(C, "_build_consensus", fake_consensus)

    from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
    bind = save_stage_snapshot(store, "L4", {"batch": ["AAA"]})
    # gating 落动作是 UPDATE 真实行，故计划必须先入库（不能只传内存 dict）
    pid = store.create_execution_plan(
        "tp1", "", "AAA", {"ticker": "AAA", "action": "buy", "shares": 10},
        snapshot_id=bind["snapshot_id"], strategy_version=bind["strategy_version"])
    plan = store.get_execution_plan(pid)
    plan.update(bind)
    async for _ in C.run_committee_review(store, [plan], budget=None, market="us_stock"):
        pass
    return store.get_execution_plan(pid)


@pytest.mark.parametrize("verdict", ["needs_review", "needs_discussion", "unknown"])
async def test_non_decisive_verdict_rejects_plan(tmp_path, monkeypatch, verdict):
    """P0-A（N-1）：needs_review/needs_discussion/unknown 不得留作 pending——否则自动执行一开就成交。

    「没人拍板」曾被当成「默认放行」：这三种裁决此前既不否决也不拦，计划以 status=pending 滞留。
    """
    store = _store(tmp_path)
    plan = await _run_committee_with_verdict(store, tmp_path, verdict, monkeypatch)
    assert plan["status"] == "rejected", verdict
    assert "结论不可背书" in plan["rejection_reason"]
    assert verdict in plan["rejection_reason"]


async def test_approved_verdict_leaves_plan_pending(tmp_path, monkeypatch):
    """反向守卫：明确通过的计划必须仍然留在 pending（不得收紧过头把正常单也拦掉）。"""
    store = _store(tmp_path)
    plan = await _run_committee_with_verdict(store, tmp_path, "approved", monkeypatch)
    assert plan["status"] == "pending"
    assert plan["rejection_reason"] == ""


async def test_non_decisive_verdict_is_not_auto_executed(tmp_path, monkeypatch):
    """端到端：needs_review 的计划走不进自动执行（committee gating + auto_execute 双闸）。"""
    from bottleneck_hunter.watchlist.auto_execute import LEVEL_SEMI, auto_execute_pending, set_auto_execute_level

    store = _store(tmp_path)
    set_auto_execute_level(store, LEVEL_SEMI)
    await _run_committee_with_verdict(store, tmp_path, "needs_review", monkeypatch)

    executed: list[str] = []

    async def fake_cae(_store, plan_id):
        executed.append(plan_id)
        return {"status": "confirmed"}

    monkeypatch.setattr("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", fake_cae)
    async for _ in auto_execute_pending(store, "us_stock"):
        pass
    assert executed == []


# ────────────────────────── (E) P2-E：独立性缺失必须有后果 ──────────────────────────

async def _run_committee_with_providers(store, providers, monkeypatch, verdict="approved"):
    """跑一遍 run_committee_review：委员 provider 按 providers 列表指定，裁决固定为 verdict。"""
    members = list(C.MEMBERS)

    async def fake_review(member, execution_plan, context):
        idx = members.index(member)
        return {"role": member["role"], "vote": "approve", "confidence": 6,
                "provider": providers[idx % len(providers)], "model": f"m{idx}"}

    async def fake_consensus(reviews, discussion, weights):
        return {"final_verdict": verdict, "summary": "测试结论",
                "consensus_modifications": [], "approval_rate": 0.75}

    monkeypatch.setattr(C, "_review_single", fake_review)
    monkeypatch.setattr(C, "_build_consensus", fake_consensus)

    from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
    bind = save_stage_snapshot(store, "L4", {"batch": ["AAA"]})
    pid = store.create_execution_plan(
        "tp1", "", "AAA", {"ticker": "AAA", "action": "buy", "shares": 10},
        snapshot_id=bind["snapshot_id"], strategy_version=bind["strategy_version"])
    plan = store.get_execution_plan(pid)
    plan.update(bind)
    async for _ in C.run_committee_review(store, [plan], budget=None, market="us_stock"):
        pass
    return store.get_execution_plan(pid)


async def test_single_provider_approved_is_blocked(tmp_path, monkeypatch):
    """P2-E（N-16）：4 位委员全在同一个 provider 上投出的 approved，不得直接放行。

    交叉验证的全部价值在「不同模型独立得出同一结论」。全挤一个 provider 时那是"1 个模型算 N 次"，
    结论与 needs_review 同类：不是"委员会说不"，而是"没有可信的委员会"。生产实测 244 笔已评审
    计划中本守卫命中 22 笔（9%），其中 6 笔因此拿到了 approved —— 这 6 笔就是本测试守的洞。
    """
    store = _store(tmp_path)
    plan = await _run_committee_with_providers(store, ["qwen"], monkeypatch)
    assert plan["status"] == "rejected"
    assert plan["rejection_reason"].startswith(store.BLOCK_MARKER_COMMITTEE)
    assert "独立性不足" in plan["rejection_reason"]
    assert "approval_rate" not in plan["rejection_reason"]  # 理由须可读，不是字段倾倒
    assert "approved" in plan["rejection_reason"]           # 原裁决带出，便于事后追溯


async def test_two_providers_approved_still_passes(tmp_path, monkeypatch):
    """反向守卫：委员分布在 ≥2 个 provider 时，approved 必须照常留在 pending。

    provider_hint 没配好就降级到同一个 provider 是常态，但只要有 2 个不同 provider，
    交叉验证就站得住 —— 拦多了等于把正常单也堵死，比不拦更糟。
    """
    store = _store(tmp_path)
    plan = await _run_committee_with_providers(store, ["qwen", "deepseek"], monkeypatch)
    assert plan["status"] == "pending"
    assert plan["rejection_reason"] == ""


async def test_single_provider_rejected_reason_stays_plain_veto(tmp_path, monkeypatch):
    """边界：全同 provider 且本就 rejected → 理由仍用「否决」，不重复叠加独立性文案。

    无条件的拦截会把 22 笔里的 13 笔既有否决改成另一种说辞，把清晰的事变模糊。
    """
    store = _store(tmp_path)
    plan = await _run_committee_with_providers(store, ["qwen"], monkeypatch, verdict="rejected")
    assert plan["status"] == "rejected"
    assert "独立性不足" not in plan["rejection_reason"]


async def test_single_provider_not_auto_executed(tmp_path, monkeypatch):
    """端到端：独立性不足的 approved 走不进自动执行（这是本修真正的收益）。"""
    from bottleneck_hunter.watchlist.auto_execute import LEVEL_SEMI, auto_execute_pending, set_auto_execute_level

    store = _store(tmp_path)
    set_auto_execute_level(store, LEVEL_SEMI)
    await _run_committee_with_providers(store, ["qwen"], monkeypatch)

    executed: list[str] = []

    async def fake_cae(_store, plan_id):
        executed.append(plan_id)
        return {"status": "confirmed"}

    monkeypatch.setattr("bottleneck_hunter.watchlist.trade_executor.confirm_and_execute", fake_cae)
    async for _ in auto_execute_pending(store, "us_stock"):
        pass
    assert executed == []


async def test_unknown_provider_is_not_treated_as_collusion(tmp_path, monkeypatch):
    """边界：provider 全空（遥测缺失）时不得拦截 —— 那是"没记下来"，不是"同源串通"。

    旧行为下 provider_hint 未落库会让 4 个空值被判为"集中于 1 个 provider"，
    于是每一笔正常 approved 都被凭空拦下。本守卫只拦有证据的同源。
    """
    store = _store(tmp_path)
    plan = await _run_committee_with_providers(store, [""], monkeypatch)
    assert plan["status"] == "pending"
    assert plan["rejection_reason"] == ""
