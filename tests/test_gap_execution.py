"""P0-3 缺口驱动补仓的建仓侧放行自检 —— 冲突的代价在前，拥挤的票后面买。

P0-2 产出的补仓意图本来会被 L4 建仓侧三道门全数跳过（已持仓 → existing_tickers；刚补过 →
5 日同向冷却；小步补仓 → 3% 建仓地板定不到仓位），也就是「算得出缺口、落不了单」。
本文件钉住放行的三个判断 + 定股函数，确保豁免只对缺口驱动的单子生效、且补到目标即停。
"""
from __future__ import annotations

from bottleneck_hunter.watchlist.decision_engine import (
    _MIN_BUILD_WEIGHT_PCT,
    _gap_driven_plans,
    _gap_fill_shares,
    _is_recent_duplicate,
    _opportunity_driven_plans,
)


def test_only_gap_driven_plans_exempted():
    """豁免面只看 L3 写的 gap_driven 标记：LLM 自己看好的低配票不因此放行。"""
    plans = [
        {"ticker": "AAPL", "result_json": {"gap_driven": True, "_planned_amount": 1666.5}},
        {"ticker": "MSFT", "result_json": {"action": "buy"}},  # LLM 择时，无标记
    ]

    got = _gap_driven_plans(plans, "us_stock")

    assert got == {"AAPL": 1666.5}


def test_gap_plan_without_amount_maps_to_zero():
    """上游算不出缺口（金额缺失/非正）→ 记 0，调用方按「无弹药」处理，而非放行一笔无上限的买。"""
    plans = [
        {"ticker": "AAPL", "result_json": {"gap_driven": True}},
        {"ticker": "MSFT", "result_json": {"gap_driven": True, "_planned_amount": -5}},
        {"ticker": "NVDA", "result_json": {"gap_driven": True, "_planned_amount": "bad"}},
    ]

    assert _gap_driven_plans(plans, "us_stock") == {"AAPL": 0.0, "MSFT": 0.0, "NVDA": 0.0}
    assert _gap_fill_shares(0.0, 100.0, 100000, 0.0, 12.0, "us_stock") == 0


def test_fill_respects_planned_amount_without_build_floor():
    """核心放行点：计划金额 400（0.4% 权益，够不到 3% 建仓地板）仍能定到股数——这就是豁免的意义。"""
    shares = _gap_fill_shares(400.0, 100.0, 100000, 0.0, 12.0, "us_stock")

    assert shares == 4  # 400/100；走 LLM 路径会被 3% 下限顶到 1200 股（金额超计划 30 倍）
    assert shares * 100.0 <= 400.0
    assert _MIN_BUILD_WEIGHT_PCT > 400.0 / 100000 * 100  # 确认本单确实低于建仓地板


def test_fill_stops_at_target_when_already_held():
    """收敛判据：现有持仓已达/超过计划金额 → 返回 0，不会天天反复小额补（防「每天刷单」）。"""
    assert _gap_fill_shares(2000.0, 100.0, 100000, 5000.0, 12.0, "us_stock") == 0
    assert _gap_fill_shares(2000.0, 100.0, 100000, 1999.0, 12.0, "us_stock") == 0  # 只剩 1 元 → 不足 1 股


def test_fill_capped_by_single_position_limit():
    """单股上限优先于计划金额：计划要补 2 万，但 12% 上限只剩 500 空间 → 只买 500。"""
    # 权益 10 万，已持 11500（11.5%），上限 12% → 剩余空间仅 500
    shares = _gap_fill_shares(20000.0, 100.0, 100000, 11500.0, 12.0, "us_stock")

    assert shares == 5
    assert shares * 100.0 == 500.0


def test_fill_a_stock_rounds_to_lot():
    """A股按 100 股整手取整：不足一手不买；A股 ticker 归一后同票仍可补。"""
    assert _gap_fill_shares(500.0, 10.0, 1000000, 0.0, 12.0, "a_stock") == 0  # 50 股不足一手
    assert _gap_fill_shares(5000.0, 10.0, 1000000, 0.0, 12.0, "a_stock") == 500


def test_duplicate_gate_still_applies_to_non_gap_plans():
    """冷却门本身没被改动：非缺口驱动的单子照旧被 5 日同向去重拦下，豁免只走 gap_plans 分支。"""
    recent = {"AAPL": [{"side": "add", "shares": 10, "date": "2026-09-22"}]}

    assert _is_recent_duplicate("add", "AAPL", recent) is True
    assert _gap_driven_plans([{"ticker": "AAPL", "result_json": {"gap_driven": True, "_planned_amount": 1}}],
                             "us_stock")
    # 同一 ticker：有无标记决定它走豁免分支还是走冷却分支
    assert "AAPL" not in _gap_driven_plans([{"ticker": "AAPL", "result_json": {"action": "add"}}], "us_stock")


def test_跨市场计划不进本市场驱动集():
    """P1-J（N-31）：跨市场守卫此前是**空操作**，等于没有守卫。

    病史：判据写成 `normalize_ticker(tk, market) != normalize_ticker(tk, tp.get("market") or market)`
    —— 同一个 ticker 拿两个市场参数各归一一次，**结果恒相等**，恒假 → 任何市场的计划都放行。
    A 股计划落进美股执行流程是真实可达的路径（L3 计划表带 market 列），必须真拦。

    （变异实证：把两条判据改回旧写法，全量套件无一报警 —— 本用例是它们的第一个护栏。）
    """
    plans = [
        {"ticker": "AAPL", "market": "a_stock", "result_json": {"gap_driven": True, "_planned_amount": 500}},
        {"ticker": "600519.SS", "market": "a_stock",
         "result_json": {"gap_driven": True, "_planned_amount": 700}},
        {"ticker": "MSFT", "market": "us_stock", "result_json": {"gap_driven": True, "_planned_amount": 900}},
        # market 缺失：按「本市场」处理（normalize_market(None) == us_stock），不得误伤
        {"ticker": "NVDA", "result_json": {"gap_driven": True, "_planned_amount": 300}},
        # 判据是 market 列，**不是** ticker 形态：美股执行流程里出现 A股形态的码，
        # 只要该行的 market 就是美股，就不该被守卫误伤（反之亦然，见上一行 AAPL）
        {"ticker": "000001.SZ", "market": "us_stock",
         "result_json": {"gap_driven": True, "_planned_amount": 100}},
    ]

    assert _gap_driven_plans(plans, "us_stock") == {"MSFT": 900.0, "NVDA": 300.0, "000001.SZ": 100.0}
    assert _gap_driven_plans(plans, "a_stock") == {"AAPL": 500.0, "600519.SS": 700.0}


def test_机会驱动的跨市场守卫同口径():
    """两条平行判据曾被写错两次，必须同时钉住 —— 只修一条会留下半个空操作。"""
    plans = [
        {"ticker": "AAPL", "market": "a_stock", "result_json": {"opportunity_driven": True, "_planned_amount": 500}},
        {"ticker": "MSFT", "market": "us_stock", "result_json": {"opportunity_driven": True, "_planned_amount": 900}},
    ]

    assert _opportunity_driven_plans(plans, "us_stock") == {"MSFT": 900.0}
    assert _opportunity_driven_plans(plans, "a_stock") == {"AAPL": 500.0}


if __name__ == "__main__":
    test_only_gap_driven_plans_exempted()
    test_gap_plan_without_amount_maps_to_zero()
    test_fill_respects_planned_amount_without_build_floor()
    test_fill_stops_at_target_when_already_held()
    test_fill_capped_by_single_position_limit()
    test_fill_a_stock_rounds_to_lot()
    test_duplicate_gate_still_applies_to_non_gap_plans()
    test_跨市场计划不进本市场驱动集()
    test_机会驱动的跨市场守卫同口径()
    print("P0-3 缺口驱动放行自检通过")
