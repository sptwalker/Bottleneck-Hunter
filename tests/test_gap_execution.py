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


# 定股调用点统一用这个包装：第 7 个参数是**绝对水位**（L2 目标持仓额），不是本轮增量。
# 这些用例关心的是金额/整手上限，水位一律给到"远在天边"（= 12% 上限口径，与 cap 同值），
# 使水位不成为本组用例的约束项；收敛判据本身由下面两条专测。
def _fill(amount, price, equity, existing, cap_pct, market, target_value=None):
    if target_value is None:
        target_value = cap_pct / 100 * equity
    return _gap_fill_shares(amount, price, equity, existing, cap_pct, market, target_value)


def test_gap_plan_without_amount_maps_to_zero():
    """上游算不出缺口（金额缺失/非正）→ 记 0，调用方按「无弹药」处理，而非放行一笔无上限的买。"""
    plans = [
        {"ticker": "AAPL", "result_json": {"gap_driven": True}},
        {"ticker": "MSFT", "result_json": {"gap_driven": True, "_planned_amount": -5}},
        {"ticker": "NVDA", "result_json": {"gap_driven": True, "_planned_amount": "bad"}},
    ]

    assert _gap_driven_plans(plans, "us_stock") == {"AAPL": 0.0, "MSFT": 0.0, "NVDA": 0.0}
    assert _fill(0.0, 100.0, 100000, 0.0, 12.0, "us_stock") == 0


def test_fill_respects_planned_amount_without_build_floor():
    """核心放行点：计划金额 400（0.4% 权益，够不到 3% 建仓地板）仍能定到股数——这就是豁免的意义。"""
    shares = _fill(400.0, 100.0, 100000, 0.0, 12.0, "us_stock")

    assert shares == 4  # 400/100；走 LLM 路径会被 3% 下限顶到 1200 股（金额超计划 30 倍）
    assert shares * 100.0 <= 400.0
    assert _MIN_BUILD_WEIGHT_PCT > 400.0 / 100000 * 100  # 确认本单确实低于建仓地板


def test_收敛看绝对水位而非本轮计划额():
    """P0-C（N-21）的**核心判据**：停手条件是"到 L2 目标"，不是"到本轮计划额"。

    旧实现拿 `planned_amount` 当水位（守卫与 planned_room 两处都减 existing_value），于是
    **任何持仓额已超过本轮小增量的票都被判「已达标」而恒返 0**。生产实测：NVDA 本轮计划
    21,859 而现有持仓 26,672 → 恒返 0，NVDA 实际权重 2.66% vs 目标 9%，缺口驱动永久冻结在
    权益 19.30%，天天跑、纹丝不动。

    本用例两半都不能少：
      · 只达本轮计划额（2000）但**远未达 L2 目标**（12% = 12000）→ **必须继续补**；
      · 已达 L2 目标 → 返 0（收敛即停，防每天刷单）。
    只留后半条的话，把源码改回旧判据（比 planned_amount）本文件仍全绿——那正是这条 bug
    能在套件全绿的情况下活到生产的原因（旧用例的名字就叫 stop_at_target_when_already_held，
    它把 bug 钉成了预期行为）。
    """
    # 权益 10 万，L2 目标 12% = 12000；已持 5000：超过本轮计划额 2000，但离目标还差 7000
    assert _gap_fill_shares(2000.0, 100.0, 100000, 5000.0, 12.0, "us_stock", 12000.0) == 20  # 补满本轮 2000
    # 已持 12000 = 达 L2 目标 → 0（再多的计划额也不动作）
    assert _gap_fill_shares(2000.0, 100.0, 100000, 12000.0, 12.0, "us_stock", 12000.0) == 0
    # 已持 11999（差 1 元 → 不足 1 股）→ 0
    assert _gap_fill_shares(2000.0, 100.0, 100000, 11999.0, 12.0, "us_stock", 12000.0) == 0


def test_本轮计划额仍封顶_不越过L2目标():
    """水位改绝对口径**不代表**金额解禁：单轮仍受本轮增量与单股上限双重封顶（防梭哈）。"""
    # 离目标还差 11000，但本轮只批了 2000 → 仍只买 2000
    assert _gap_fill_shares(2000.0, 100.0, 100000, 1000.0, 12.0, "us_stock", 12000.0) == 20
    # 本轮批了 20000，但离目标只剩 11000 → 只买到目标为止（多出的 9000 不追）
    assert _gap_fill_shares(20000.0, 100.0, 100000, 1000.0, 12.0, "us_stock", 12000.0) == 110


def test_fill_capped_by_single_position_limit():
    """单股上限优先于计划金额：计划要补 2 万，但 12% 上限只剩 500 空间 → 只买 500。"""
    # 权益 10 万，已持 11500（11.5%），上限 12% → 剩余空间仅 500（水位给到 12%，与该上限同值）
    shares = _fill(20000.0, 100.0, 100000, 11500.0, 12.0, "us_stock", 12000.0)

    assert shares == 5
    assert shares * 100.0 == 500.0


def test_fill_a_stock_rounds_to_lot():
    """A股按 100 股整手取整：不足一手不买；A股 ticker 归一后同票仍可补。"""
    assert _fill(500.0, 10.0, 1000000, 0.0, 12.0, "a_stock") == 0  # 50 股不足一手
    assert _fill(5000.0, 10.0, 1000000, 0.0, 12.0, "a_stock") == 500


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
    test_收敛看绝对水位而非本轮计划额()
    test_本轮计划额仍封顶_不越过L2目标()
    test_fill_capped_by_single_position_limit()
    test_fill_a_stock_rounds_to_lot()
    test_duplicate_gate_still_applies_to_non_gap_plans()
    test_跨市场计划不进本市场驱动集()
    test_机会驱动的跨市场守卫同口径()
    print("P0-3 缺口驱动放行自检通过")
