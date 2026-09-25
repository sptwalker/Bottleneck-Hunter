"""P0-4 机会/信念驱动器自检 —— 第二股扩张力：敢越过 L2 目标，但绝不越过硬红线。

对应 docs/DECISION_CHAIN_REVIEW_2026-09.md P0-4 的验证要求：
① "构造一只期望上行 40%+ 催化剂临近的票 + 权益已达 equity_max 的账户 → 断言生成带
   mandate_exception 的买入意图、金额在战术缓冲带内、且被路由到投委会而非自动执行"；
② "构造越单票硬上限的场景 → 断言即使顶档信念也被 C 类拦死"。

信念分与档位是确定性纯函数（无 LLM），所以这里全部用纯函数 + 假 Store 钉住。
"""
from __future__ import annotations

from bottleneck_hunter.watchlist.decision_engine import (
    _OPPORTUNITY_ROUND_CAP,
    _conviction_score,
    _gap_fill_shares,
    _opportunity_driven_plans,
    _opportunity_tier,
    _plan_opportunity_fills,
)
from bottleneck_hunter.watchlist.store_base import _now_iso

# 期望上行 40%（顶档满分）+ 3 天后 high 催化剂 + 观察池 80 分 → 顶档
BIG_UPSIDE = {"expected_return_pct": 40.0}


def _cat(days: int = 3, impact: str = "high") -> dict:
    return {"days_left": days, "impact_level": impact, "title": "三期产能投产"}


# ───────────────────────── 信念分：单调、有界、来源可解释 ─────────────────────────

def test_conviction_monotonic_in_each_input():
    """三个输入各自单调：上行越高 / 催化剂越近越强 / 评分越高 → 信念分越高。"""
    base = _conviction_score(20.0, 15, "medium", 50.0)

    assert _conviction_score(40.0, 15, "medium", 50.0) > base        # 上行更高
    assert _conviction_score(20.0, 1, "medium", 50.0) > base         # 催化剂更近
    assert _conviction_score(20.0, 15, "critical", 50.0) > base      # 影响力更强
    assert _conviction_score(20.0, 15, "medium", 95.0) > base        # 观察池评分更高


def test_conviction_bounded_and_handles_junk():
    """0~1 有界；负上行/缺字段/垃圾值都不炸、不退化成 NaN。"""
    assert _conviction_score(999.0, 0, "critical", 100.0) <= 1.0
    assert _conviction_score(-30.0, None, "", 0.0) >= 0.0
    assert _conviction_score(None, None, None, None) == 0.0


def test_top_tier_needs_all_three_legs():
    """顶档要三腿齐备：单靠高上行（无催化剂/无评分）够不到顶档，防"只要 PE 便宜就梭哈"。"""
    assert _opportunity_tier(_conviction_score(40.0, None, "", 0.0)) is None or \
        _opportunity_tier(_conviction_score(40.0, None, "", 0.0))[0] < 8.0
    assert _opportunity_tier(_conviction_score(40.0, 3, "high", 80.0))[0] == 8.0


def test_tier_table_is_monotonic():
    """档位表自身单调：分数越高，允许的目标权重与单轮金额都不更小（防手改表时写反）。"""
    tiers = [_opportunity_tier(s) for s in (0.9, 0.7, 0.5)]
    assert all(t is not None for t in tiers)
    for higher, lower in zip(tiers, tiers[1:], strict=False):
        assert higher[0] >= lower[0] and higher[1] >= lower[1]


# ───────────────────────── 场景一：越线意图在缓冲带内 ─────────────────────────

def test_high_conviction_produces_bounded_intent():
    """验收①：40% 上行 + 临近催化剂 → 产出买意图，金额落在档位/单轮预算内（不是梭哈）。"""
    account = {"total_equity": 100000, "cash_balance": 40000}
    valuations = {"NVDA": BIG_UPSIDE}

    fills = _plan_opportunity_fills(account, [], valuations, {"NVDA": _cat()}, {"NVDA": 80.0}, "us_stock")

    assert len(fills) == 1
    f = fills[0]
    assert f["action"] == "buy"                      # 未持仓 → 新建
    assert f["amount"] <= 100000 * _OPPORTUNITY_ROUND_CAP + 1e-6  # 单轮预算封顶
    assert f["amount"] <= 0.04 * 100000 + 1e-6       # 顶档单轮 4% 权益封顶
    assert f["target_weight_pct"] == 8.0             # 越过了 L2 目标（示例：L2 给 5%）


def test_already_at_tier_target_stops():
    """同一信念不会无限加：已达档位目标权重 → 不再产意图（这是机会驱动自己的收敛判据）。"""
    account = {"total_equity": 100000, "cash_balance": 40000}
    positions = [{"ticker": "NVDA", "market_value": 8000.0}]  # 8% = 顶档目标

    assert _plan_opportunity_fills(account, positions, {"NVDA": BIG_UPSIDE},
                                   {"NVDA": _cat()}, {"NVDA": 80.0}, "us_stock") == []


def test_low_conviction_produces_nothing():
    """信念不足最低档 → 不出手（不是"每个有估值数据的票都买一点"）。"""
    account = {"total_equity": 100000, "cash_balance": 40000}
    vals = {"AAPL": {"expected_return_pct": 5.0}}  # 弱上行 + 无催化剂 + 无评分

    assert _plan_opportunity_fills(account, [], vals, {}, {}, "us_stock") == []


def test_round_budget_shared_across_tickers():
    """单轮预算在多票之间共享：信念最高的先拿，总和不超 _OPPORTUNITY_ROUND_CAP。"""
    account = {"total_equity": 100000, "cash_balance": 100000}
    vals = {"NVDA": BIG_UPSIDE, "AMD": {"expected_return_pct": 38.0}, "MU": {"expected_return_pct": 36.0}}
    cats = {"NVDA": _cat(1), "AMD": _cat(2), "MU": _cat(3)}
    comps = {"NVDA": 90.0, "AMD": 85.0, "MU": 80.0}

    fills = _plan_opportunity_fills(account, [], vals, cats, comps, "us_stock")

    assert sum(f["amount"] for f in fills) <= 100000 * _OPPORTUNITY_ROUND_CAP + 1e-6
    assert fills[0]["ticker"] == "NVDA"  # 信念最高者优先
    assert [f["score"] for f in fills] == sorted((f["score"] for f in fills), reverse=True)


def test_a_stock_ticker_normalized_against_positions():
    """A股归一口径与持仓一致：600519 持仓 + 600519.SS 估值 → 识别为 add 而非重复新建。"""
    account = {"total_equity": 1000000, "cash_balance": 900000}
    positions = [{"ticker": "600519", "market_value": 10000.0}]

    fills = _plan_opportunity_fills(account, positions, {"600519.SS": BIG_UPSIDE},
                                    {"600519.SS": _cat()}, {"600519.SS": 80.0}, "a_stock")

    assert len(fills) == 1
    assert fills[0]["action"] == "add"


# ───────────────────────── 场景二：硬红线即使顶档信念也拦得住 ─────────────────────────

def test_top_conviction_blocked_by_single_position_hard_cap():
    """验收②：单票硬上限 3%、顶档信念 → 定股为 0，被硬顶拦死（越线只在"目标权重"维度，硬顶不让）。"""
    # 权益 10 万，已持 2900（2.9%），硬顶 3% → 只剩 100 元空间。水位取档位上限之上（8%），
    # 即「档位允许追高，但硬顶说了算」——水位传 4000 会提前收敛，测不到硬顶这道红线。
    shares = _gap_fill_shares(4000.0, 100.0, 100000, 2900.0, 3.0, "us_stock", 8000.0)

    assert shares == 1  # 只剩 100 元 = 1 股；档位想买 4000 元也顶不动硬顶
    assert shares * 100.0 <= 3000.0 - 2900.0


def test_hard_cap_already_reached_yields_zero():
    """已达硬顶 → 顶档信念也定不到任何股数（无残留空间）。"""
    assert _gap_fill_shares(4000.0, 100.0, 100000, 3000.0, 3.0, "us_stock", 8000.0) == 0


def test_机会驱动的水位不被L2承诺掐死():
    """P0-4 的命门：机会驱动**生来就要越过 L2 目标**。若水位被取成 L2 承诺（如 6%），
    已持 5% 的票距水位只剩 1%，档位想追到 8% 的额度就永远用不上——越线再也发生不了，
    等于把这个驱动器关掉（这是 L4 调用点必须按 opportunity 分支取水位的原因）。
    """
    # 权益 10 万、已持 5000（5%）、硬顶 12%、档位上限 8%（=8000 元）
    # 水位取档位 8% → 还能补 3000；水位若误取 L2 承诺 6% → 只能补 1000
    assert _gap_fill_shares(4000.0, 100.0, 100000, 5000.0, 12.0, "us_stock", 8000.0) == 30
    assert _gap_fill_shares(4000.0, 100.0, 100000, 5000.0, 12.0, "us_stock", 6000.0) == 10


def test_no_cash_means_no_intent():
    """可用现金是 C 类红线：无现金 → 不出意图（即便信念顶档）。"""
    account = {"total_equity": 100000, "cash_balance": 0}

    assert _plan_opportunity_fills(account, [], {"NVDA": BIG_UPSIDE},
                                   {"NVDA": _cat()}, {"NVDA": 80.0}, "us_stock") == []


# ───────────────────────── L4 识别：越线必须上会、必须留痕 ─────────────────────────

def test_only_opportunity_marked_plans_are_routed():
    """只认 L3 自己写的 opportunity_driven 标记：LLM 看好的票不会因此被当成越线强行上会。"""
    plans = [
        {"ticker": "NVDA", "result_json": {"opportunity_driven": True, "_planned_amount": 3000.0}},
        {"ticker": "AMD", "result_json": {"action": "buy", "_planned_amount": 3000.0}},
        {"ticker": "MU", "result_json": {"gap_driven": True, "_planned_amount": 3000.0}},
    ]

    assert _opportunity_driven_plans(plans, "us_stock") == {"NVDA": 3000.0}


def test_both_drivers_share_the_entry_gate_exemption():
    """共用建仓侧豁免（都是"有金额上限的系统补仓"，不是 LLM 择时），但可分辨性必须保住：

    `_gap_driven_plans` 是两类驱动的**并集**（豁免建仓侧门禁）；上会分流靠 `_opportunity_driven_plans`
    单认机会驱动——若哪天有人把后者也改成并集，"哪笔要人工确认"就丢了。
    """
    plans = [{"ticker": "NVDA", "result_json": {"opportunity_driven": True, "_planned_amount": 100.0}}]
    from bottleneck_hunter.watchlist.decision_engine import _gap_driven_plans

    assert _gap_driven_plans(plans, "us_stock") == {"NVDA": 100.0}      # 并集：豁免照给
    assert _opportunity_driven_plans(plans, "us_stock") == {"NVDA": 100.0}  # 单认：需上会

    gap_only = [{"ticker": "MU", "result_json": {"gap_driven": True, "_planned_amount": 100.0}}]

    assert _gap_driven_plans(gap_only, "us_stock") == {"MU": 100.0}
    assert _opportunity_driven_plans(gap_only, "us_stock") == {}        # 缺口驱动不必上会


def test_mandate_exception_never_auto_executed():
    """护栏最外层：带 mandate_exception 的执行计划不进自动执行集合（人工确认不可绕过）。"""
    from bottleneck_hunter.watchlist.auto_execute import _is_mandate_exception

    assert _is_mandate_exception({"result_json": {"mandate_exception": True}}) is True
    assert _is_mandate_exception({"result_json": {"action": "buy"}}) is False
    assert _is_mandate_exception({"result_json": '{"mandate_exception": true}'}) is True  # 原始串也认
    assert _is_mandate_exception({"result_json": "not json"}) is False


# ───────────────────────── 落库层：意图成为带标记的 L3 计划 ─────────────────────────

class _FakeStore:
    """只提供 _generate_opportunity_driven_plans 需要的读方法（不碰 DB）。"""

    def __init__(self, account, positions=(), entries=(), valuations=(), catalysts=(), macro=None):
        self._account, self._positions, self._entries = account, list(positions), list(entries)
        self._valuations, self._catalysts = dict(valuations), list(catalysts)
        self._macro = macro if macro is not None else {
            "result_json": {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5},
            "created_at": _now_iso(),
        }
        self.plans: list[dict] = []

    def get_latest_macro_strategy(self):
        return self._macro

    def get_sim_account(self, account_ref=""):
        return self._account

    def get_sim_positions(self, account_id=None, include_zero=False):
        return self._positions

    def get_valuation_map(self):
        return self._valuations

    def get_recent_catalysts(self, days_ahead=14, days_back=7, limit=8):
        return self._catalysts

    def list_all(self):
        return self._entries

    def create_tactical_plan(self, strategic_plan_id, entry_id, ticker, plan_date, result_json, **kw):
        self.plans.append({"ticker": ticker, "entry_id": entry_id, "result_json": result_json, **kw})
        return f"plan-{ticker}"


def _fresh(monkeypatch):
    import bottleneck_hunter.watchlist.decision_engine as de
    monkeypatch.setattr(de, "save_stage_snapshot", lambda store, stage, inputs: {"snapshot_id": None})
    monkeypatch.setattr(de, "_decision_provenance", lambda *a, **k: {"layer": "L3"})


def test_wrapper_writes_marked_plans(monkeypatch):
    """落库层：计划必须带 opportunity_driven + mandate_exception + _planned_amount 三件套。"""
    _fresh(monkeypatch)
    from bottleneck_hunter.watchlist.decision_engine import _generate_opportunity_driven_plans

    # 权益已到 regime 上限（equity_max=60）仍满仓现金 → 越线意图照样产出，交给投委会处置
    store = _FakeStore(
        {"id": "acct9", "total_equity": 100000, "cash_balance": 40000},
        entries=[{"ticker": "NVDA", "id": "e9", "composite_score": 80.0}],
        valuations={"NVDA": BIG_UPSIDE},
        catalysts=[{**_cat(), "ticker": "NVDA", "expected_date": "2026-09-26"}],
    )
    ids = _generate_opportunity_driven_plans(store, "us_stock",
                                            {"id": "sp1", "created_at": _now_iso(), "result_json": {}})

    assert ids == ["plan-NVDA"]
    rj = store.plans[0]["result_json"]
    assert rj["opportunity_driven"] is True
    assert rj["mandate_exception"] is True   # 越线显式申报 → L4 强制人工确认
    assert rj["_planned_amount"] > 0


def test_wrapper_skips_without_valuations(monkeypatch):
    """无估值数据 = 无信念依据 → 不出手（不猜、不默认给仓位）。"""
    _fresh(monkeypatch)
    from bottleneck_hunter.watchlist.decision_engine import _generate_opportunity_driven_plans

    store = _FakeStore({"id": "a", "total_equity": 100000, "cash_balance": 40000})
    ids = _generate_opportunity_driven_plans(store, "us_stock",
                                             {"id": "sp1", "created_at": _now_iso(), "result_json": {}})

    assert ids == []
    assert store.plans == []


def test_wrapper_stops_on_stale_upstream(monkeypatch, caplog):
    """上游陈旧（>8 天）→ 机会驱动也停：不在陈旧数据上追高。"""
    _fresh(monkeypatch)
    import logging

    from bottleneck_hunter.watchlist.decision_engine import _generate_opportunity_driven_plans

    store = _FakeStore({"id": "a", "total_equity": 100000, "cash_balance": 40000},
                       valuations={"NVDA": BIG_UPSIDE})
    with caplog.at_level(logging.WARNING, logger="bottleneck_hunter.watchlist.decision_engine"):
        ids = _generate_opportunity_driven_plans(
            store, "us_stock",
            {"id": "sp1", "created_at": "2026-01-01T00:00:00+00:00", "result_json": {}})

    assert ids == []
    assert store.plans == []
    assert "陈旧" in caplog.text


if __name__ == "__main__":
    test_conviction_monotonic_in_each_input()
    test_conviction_bounded_and_handles_junk()
    test_top_tier_needs_all_three_legs()
    test_tier_table_is_monotonic()
    test_high_conviction_produces_bounded_intent()
    test_already_at_tier_target_stops()
    test_low_conviction_produces_nothing()
    test_round_budget_shared_across_tickers()
    test_a_stock_ticker_normalized_against_positions()
    test_top_conviction_blocked_by_single_position_hard_cap()
    test_hard_cap_already_reached_yields_zero()
    test_no_cash_means_no_intent()
    test_only_opportunity_marked_plans_are_routed()
    test_both_drivers_share_the_entry_gate_exemption()
    test_mandate_exception_never_auto_executed()
    print("P0-4 机会驱动器自检通过")
