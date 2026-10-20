"""P0-2 缺口驱动器自检 — "配置不足"如何变成买入意图（纯确定性，无 LLM）。

对应 docs/DECISION_CHAIN_REVIEW_2026-09.md P0-2 的验证要求：
"构造现金 90% + L2 目标权益 60% 的账户，跑一轮，断言生成了指向低配核心票的买入意图且总额 ≤ 可用现金"。
外加三条硬约束的回归：只在 equity < equity_min 触发、单轮步长上限、收敛即停。
"""
from __future__ import annotations

from bottleneck_hunter.watchlist.decision_engine import (
    _GAP_STEP_FRACTION,
    _generate_gap_driven_plans,
    _plan_gap_fills,
)
from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds
from bottleneck_hunter.watchlist.store import WatchlistStore

# sideways/balanced → equity 40~60，单票上限 12
BOUNDS = {"equity_min": 40, "equity_max": 60, "cash_max": 40, "max_single_pct": 12}
CORE = [{"ticker": "AAPL", "target_weight_pct": 12.0}, {"ticker": "MSFT", "target_weight_pct": 10.0}]


def test_cash_90pct_target_60pct_yields_buy_intents_within_deployable():
    """P0-2 验收场景：现金 90%、目标权益 60% → 产出指向低配核心票的买意图，总额 ≤ 可部署现金。"""
    account = {"total_equity": 100000, "cash_balance": 90000}
    positions = [{"ticker": "AAPL", "market_value": 10000}]  # 权益 10%，缺口 30pct → 名义 30000

    fills = _plan_gap_fills(account, positions, BOUNDS, CORE, "us_stock")

    by_ticker = {f["ticker"]: f for f in fills}
    assert set(by_ticker) == {"AAPL", "MSFT"}
    assert by_ticker["AAPL"]["action"] == "add"   # 已持仓 → add
    assert by_ticker["MSFT"]["action"] == "buy"   # 未持仓 → buy
    assert fills[0]["ticker"] == "MSFT"           # 按缺口倒序：MSFT 缺口 10pct > AAPL 剩余空间 2pct
    total = sum(f["amount"] for f in fills)
    assert total <= 30000.0                        # ≤ 可部署现金（缺口名义，此处现金充足）
    assert total <= 30000.0 * _GAP_STEP_FRACTION + 1e-6  # 且 ≤ 单轮步长上限（防梭哈）
    # 单票不越过 L2 目标权重：AAPL 已有 10000，最多再补 (12-10)% × 100000 = 2000
    assert by_ticker["AAPL"]["amount"] <= 2000.0


def test_step_cap_bounds_single_round():
    """整仓现金、缺口 55pct → 单轮只补 1/3 缺口（且被单票目标权重封顶），余额留给后续轮次。"""
    account = {"total_equity": 100000, "cash_balance": 100000}

    fills = _plan_gap_fills(account, [], BOUNDS, CORE, "us_stock")

    total = sum(f["amount"] for f in fills)
    # 预算 100000×55%×1/3 = 18333，但两只票"补到目标"合计只装得下 (12+10)%×100000 = 22000；
    # 分到 AAPL 的 10575 被目标权重封顶为 12000 → 实际 10575 → 合计 13332
    assert total == 13332.0
    assert total < 100000 * 0.55  # 远小于整个缺口


def test_no_fills_when_at_or_above_floor():
    """权益已达下限 → 缺口为零，驱动器不产任何意图（收敛判据：不会每天反复补）。"""
    account = {"total_equity": 100000, "cash_balance": 40000}
    positions = [{"ticker": "AAPL", "market_value": 60000}]

    assert _plan_gap_fills(account, positions, BOUNDS, CORE, "us_stock") == []


def test_no_fills_when_no_cash():
    """有缺口但无现金 → 无可部署弹药，不下单。"""
    account = {"total_equity": 100000, "cash_balance": 0}
    assert _plan_gap_fills(account, [], BOUNDS, CORE, "us_stock") == []


def test_ticker_normalization_matches_held_position():
    """600519 与 600519.SS 视为同票：A股持仓能正确识别为"加仓"而非"新建"。"""
    bounds = {"equity_min": 40, "equity_max": 60, "max_single_pct": 12}
    core = [{"ticker": "600519.SS", "target_weight_pct": 12.0}]
    account = {"total_equity": 1000000, "cash_balance": 950000}
    positions = [{"ticker": "600519", "market_value": 50000}]

    fills = _plan_gap_fills(account, positions, bounds, core, "a_stock")

    assert len(fills) == 1
    assert fills[0]["action"] == "add"
    assert fills[0]["current_weight_pct"] == 5.0


def test_target_weight_missing_or_zero_skipped():
    """L2 没给目标权重的票不入候选（驱动器只补"有目标"的核心票）。"""
    core = [{"ticker": "AAPL", "target_weight_pct": 0}, {"ticker": "MSFT"}]
    account = {"total_equity": 100000, "cash_balance": 90000}

    assert _plan_gap_fills(account, [], BOUNDS, core, "us_stock") == []


# ───────────────── 落库层：意图确实成为 L3 战术计划，且账户标识进日志 ─────────────────

class _FakeStore:
    """只提供 _generate_gap_driven_plans 需要的那几个读方法（不碰 DB）。"""

    def __init__(self, account, positions=(), entries=(), macro=None):
        self._account, self._positions, self._entries = account, list(positions), list(entries)
        self._macro = macro if macro is not None else {
            "result_json": {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5},
            "created_at": _now(),
        }
        self.plans: list[dict] = []

    def get_latest_macro_strategy(self):
        return self._macro

    def get_sim_account(self, account_ref=""):
        return self._account

    def get_sim_positions(self, account_id=None, include_zero=False):
        return self._positions

    def list_all(self):
        return self._entries

    def create_tactical_plan(self, strategic_plan_id, entry_id, ticker, plan_date, result_json, **kw):
        self.plans.append({"ticker": ticker, "entry_id": entry_id, "result_json": result_json, **kw})
        return f"plan-{ticker}"


def _fresh(monkeypatch):
    import bottleneck_hunter.watchlist.decision_engine as de
    monkeypatch.setattr(de, "save_stage_snapshot", lambda store, stage, inputs: {"snapshot_id": None})
    monkeypatch.setattr(de, "_decision_provenance", lambda *a, **k: {"layer": "L3"})


def test_wrapper_writes_plans_and_logs_account_id(monkeypatch, caplog):
    """落库层：计划带 gap_driven 标记与 _planned_amount，日志带账户 id 便于多账户分辨。"""
    _fresh(monkeypatch)
    import logging

    store = _FakeStore(
        {"id": "acct123", "total_equity": 100000, "cash_balance": 90000},
        positions=[{"ticker": "AAPL", "market_value": 10000}],
        entries=[{"ticker": "AAPL", "id": "e1"}, {"ticker": "MSFT", "id": "e2"}],
    )
    with caplog.at_level(logging.INFO, logger="bottleneck_hunter.watchlist.decision_engine"):
        ids = _generate_gap_driven_plans(
            store, "us_stock",
            {"id": "sp1", "created_at": _now(), "result_json": {"stock_selection": {"core_holdings": CORE}}},
        )

    assert ids == ["plan-MSFT", "plan-AAPL"]  # 缺口倒序：MSFT(10pct) 先于 AAPL(2pct)
    assert all(p["result_json"]["gap_driven"] is True for p in store.plans)
    assert store.plans[1]["entry_id"] == "e1"          # AAPL 已入观察池 → 关联 entry
    # 按缺口比例分摊步长预算。**不要把这个数写死**：预算 = 缺口×(1/3)、缺口又由生效权益下限决定，
    # 而 N-11 之后下限随 regime_confidence 收缩（sideways/balanced 置信度 5 → 31.7，而非表内原始 40）。
    # 硬编码 1666.5 会在任何置信度口径调整后再断一次；改为同源推导，断则说明分摊逻辑真变了。
    _floor = get_allocation_bounds("sideways", "balanced", 5)["equity_min"]  # L1 fixture 的 regime/置信度
    _gap_dollars = (_floor - 10.0) / 100 * 100000  # 权益 10% → 缺口 dollars
    _budget = _gap_dollars * _GAP_STEP_FRACTION
    assert store.plans[1]["result_json"]["_planned_amount"] == round(_budget * 2 / 12, 2)  # AAPL 缺口 2pct / 合计 12pct
    assert "acct123" in caplog.text  # 多账户同机跑时，光看金额分辨不出是哪本账


def test_wrapper_stops_on_stale_upstream(monkeypatch, caplog):
    """上游陈旧（>8 天）→ 确定性补仓也停，不在陈旧数据上补仓。"""
    _fresh(monkeypatch)
    import logging

    old = "2026-01-01T00:00:00+00:00"
    store = _FakeStore({"id": "a", "total_equity": 100000, "cash_balance": 90000})
    with caplog.at_level(logging.WARNING, logger="bottleneck_hunter.watchlist.decision_engine"):
        ids = _generate_gap_driven_plans(
            store, "us_stock",
            {"id": "sp1", "created_at": old, "result_json": {"stock_selection": {"core_holdings": CORE}}},
        )

    assert ids == []
    assert store.plans == []
    assert "陈旧" in caplog.text


def _now() -> str:
    from bottleneck_hunter.watchlist.store_base import _now_iso
    return _now_iso()


def _seed(tmp_path, name):
    return WatchlistStore(str(tmp_path / name)).for_user("u1").for_market("us_stock")


def test_end_to_end_store_roundtrip(tmp_path):
    """真 Store 落库：缺口驱动产出的 tactical_plans 可被当日查询读回（显式 db_path，不碰生产库）。"""
    store = _seed(tmp_path, "gap.db")
    macro_id = store.create_macro_strategy(
        {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False)
    store.create_strategic_plan(
        macro_id, {"stock_selection": {"core_holdings": CORE}, "target_allocation": {"equity_pct": 60}}, strict=False)
    account = store.get_sim_account()
    store.update_sim_account(total_equity=100000, current_capital=100000, cash_balance=100000)
    for tk in ("AAPL", "MSFT"):
        store.add({"ticker": tk, "company_name": tk, "market": "us_stock", "tier": "focus"})

    strategic = store.get_latest_strategic_plan()
    ids = _generate_gap_driven_plans(store, "us_stock", strategic)

    assert ids, "缺口 40pct + 满仓现金 → 应产出补仓计划"
    rows = store.get_tactical_plans_by_date()
    assert {r["ticker"] for r in rows} == {"AAPL", "MSFT"}
    assert all(r["action"] in ("add", "buy") for r in rows)
    # 驱动器只产意图、不下单也不动账：账户现金原样未变（下单是 L4 的事）
    assert store.get_sim_account()["cash_balance"] == 100000
    assert account["id"] == store.get_sim_account()["id"]


if __name__ == "__main__":
    test_cash_90pct_target_60pct_yields_buy_intents_within_deployable()
    test_step_cap_bounds_single_round()
    test_no_fills_when_at_or_above_floor()
    test_no_fills_when_no_cash()
    test_ticker_normalization_matches_held_position()
    test_target_weight_missing_or_zero_skipped()
    print("P0-2 缺口驱动器自检通过")


def test_wrapper_drops_core_out_of_pool(monkeypatch):
    """L2 核心持仓含观察池外标的（存量计划/LLM 越界）→ 缺口驱动不为它补仓。"""
    _fresh(monkeypatch)
    store = _FakeStore({"id": "a", "total_equity": 100000, "cash_balance": 90000},
                       entries=[{"ticker": "AAPL", "id": "e1"}])
    ids = _generate_gap_driven_plans(
        store, "us_stock",
        {"id": "sp1", "created_at": _now(), "result_json": {"stock_selection": {"core_holdings": CORE}}},
    )
    assert ids == ["plan-AAPL"]  # MSFT 不在池 → 剔除


# ── P0-1（2026-10 体检）：tactical 持仓也参与缺口补齐 ──

def test_tactical_holdings_participate_half_weight():
    """核心仅 1 只、tactical 多只时，确定性通道要能覆盖到 tactical，且按系数打折、
    水位仍报 L2 原始目标（L4 判"离目标多远"不能拿折后值当承诺）。"""
    from bottleneck_hunter.watchlist.decision_engine import _GAP_TACTICAL_FACTOR

    account = {"total_equity": 100000, "cash_balance": 100000}
    core = [{"ticker": "AAA", "target_weight_pct": 8.0}]
    tactical = [{"ticker": "BBB", "target_weight_pct": 10.0}, {"ticker": "CCC", "target_weight_pct": 6.0}]

    fills = _plan_gap_fills(account, [], BOUNDS, core, "us_stock", tactical_holdings=tactical)
    by = {f["ticker"]: f for f in fills}
    assert set(by) == {"AAA", "BBB", "CCC"}          # 三只都进入确定性通道
    assert by["BBB"]["target_weight_pct"] == 10.0    # 水位 = L2 原始目标，不是折后 5%
    assert by["BBB"]["amount"] <= 10.0 * _GAP_TACTICAL_FACTOR / 100 * 100000 + 1e-6
    # 不打折时会补满 10%：对比证明打折生效
    full = _plan_gap_fills(account, [], BOUNDS, core + tactical, "us_stock")
    assert {f["ticker"]: f for f in full}["BBB"]["amount"] > by["BBB"]["amount"]


def test_tactical_not_invented_when_at_target():
    """tactical 已达（折后）目标 → 不产单。"""
    account = {"total_equity": 100000, "cash_balance": 60000}
    held = [{"ticker": "BBB", "market_value": 5200}]  # 5.2% > 折后目标 10%×0.5
    fills = _plan_gap_fills(account, held, BOUNDS, [], "us_stock",
                            tactical_holdings=[{"ticker": "BBB", "target_weight_pct": 10.0}])
    assert fills == []


# ── P0-2（2026-10 体检）：买入计划必须带止损，硬止损巡检优先读它 ──

def _mk_store(tmp_path):
    return WatchlistStore(str(tmp_path / "stop.db")).for_user("u1").for_market("us_stock")


def test_attach_stop_loss_prefers_l3_exit_plan(tmp_path):
    from bottleneck_hunter.watchlist.decision_engine import _attach_stop_loss

    store = _mk_store(tmp_path)
    store.create_tactical_plan("", "", "AAA", "2026-10-19",
                               {"ticker": "AAA", "action": "buy",
                                "exit_plan": {"stop_loss": {"price": 90.0}}}, strict=False)
    ep = _attach_stop_loss(store, {"ticker": "AAA", "action": "buy"}, "", 100.0, "us_stock")
    assert ep["stop_loss"]["price"] == 90.0 and ep["stop_loss"]["source"] == "L3 exit_plan"


def test_attach_stop_loss_defaults_and_skips_sells(tmp_path):
    from bottleneck_hunter.watchlist.decision_engine import _attach_stop_loss

    store = _mk_store(tmp_path)
    ep = _attach_stop_loss(store, {"ticker": "BBB", "action": "add"}, "", 100.0, "a_stock")
    assert ep["stop_loss"]["pct"] == -10.0  # A股默认幅度
    sell = _attach_stop_loss(store, {"ticker": "BBB", "action": "sell"}, "", 100.0, "a_stock")
    assert "stop_loss" not in sell


def test_attach_stop_loss_rejects_stop_above_price(tmp_path):
    from bottleneck_hunter.watchlist.decision_engine import _attach_stop_loss

    store = _mk_store(tmp_path)
    store.create_tactical_plan("", "", "CCC", "2026-10-19",
                               {"ticker": "CCC", "action": "buy",
                                "exit_plan": {"stop_loss": {"price": 120.0}}}, strict=False)
    ep = _attach_stop_loss(store, {"ticker": "CCC", "action": "buy"}, "", 100.0, "us_stock")
    assert ep["stop_loss"]["price"] < 100.0  # 高于现价的止损 → 退回默认 8%
    assert ep["stop_loss"]["source"].startswith("默认")


def test_hard_stop_sweep_uses_execution_plan_stop(tmp_path):
    """止损挂在执行计划（无 L3 exit_plan）时，巡检仍要触发清仓卖单。"""
    import asyncio

    from bottleneck_hunter.watchlist.decision_engine import _hard_stop_loss_sweep

    store = _mk_store(tmp_path)
    acct = store.get_sim_account()
    store.create_sim_position(acct["id"], "AAA", 100, 100.0)
    store.save_snapshots([{"ticker": "AAA", "date": "2026-10-19", "close": 85.0,
                           "market": "us_stock", "data_quality": "ok", "quality_notes": ""}])
    store.create_execution_plan("", "", "AAA",
                                {"action": "buy", "shares": 100, "stop_loss": {"price": 90.0}},
                                status="executed", strict=False)
    plan = store.get_latest_tactical_plan_for_ticker("AAA")
    assert plan is None  # 无 L3 计划，止损只能来自执行计划

    async def _run():
        return [e async for e in _hard_stop_loss_sweep(store, "us_stock")]

    asyncio.run(_run())
    sells = [e for e in store.get_pending_executions() if (e.get("result_json") or {}).get("_hard_stop")]
    if (store.get_sim_positions(acct["id"]) or []):
        assert sells and sells[0]["ticker"] == "AAA"


# ── P2-2（2026-10 体检）：同一 ticker 同日只保留一条 active 战术计划 ──

def test_same_day_duplicate_tactical_superseded(tmp_path):
    store = _mk_store(tmp_path)
    a = store.create_tactical_plan("", "", "AAA", "2026-10-19",
                                   {"ticker": "AAA", "action": "add", "exit_plan": {}}, strict=False)
    b = store.create_tactical_plan("", "", "AAA", "2026-10-19",
                                   {"ticker": "AAA", "action": "buy",
                                    "exit_plan": {"stop_loss": {"price": 90.0}}}, strict=False)
    active = store.get_tactical_plans_by_date("2026-10-19")
    assert [p["id"] for p in active] == [b]                 # 只留最新一条
    assert [p["status"] for p in store.get_tactical_plans_by_date("2026-10-19", include_inactive=True)
            if p["id"] == a] == ["cancelled"]
    latest = store.get_latest_tactical_plan_for_ticker("AAA")
    assert latest["id"] == b and latest["exit_plan"]["stop_loss"]["price"] == 90.0
    # 留痕可查
    assert {p["id"] for p in store.get_tactical_plans_by_date("2026-10-19", include_inactive=True)} == {a, b}
