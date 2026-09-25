"""P1-B：缺口驱动器的端到端收敛验收（此前从未做过，是 P0-C 的验收判据）。

病历：`test_gap_driver.py` 只测 L3（意图怎么算出来），`test_gap_execution.py` 只测 L4（意图怎么定成股），
**两半从未联合跑过**。于是「L3 算出意图 → L4 定股 → 组合真的朝目标走过去」这条闭环没有任何护栏，
套件全绿而组合在生产上**永久卡在 19.30%**（见 `_gap_fill_shares` 的 N-21）。

本文件按真实路径串起来跑：每轮取 `_plan_gap_fills`（L3 真实意图）→ 逐票 `_gap_fill_shares`
（L4 真实定股）→ 把成交回写账户与持仓 → 下一轮。断言三件事（报告 P1-B 原文）：

1. 权益占比**单调逼近**目标带内 —— 且**真的进了带**（不是无限逼近）；
2. 停在带内后**不再产生新计划**（不刷单）；
3. 每轮总额 ≤ 步长上限。

**防恒真**：本文件最容易退化成"循环体一次都没进、断言全绿"。故每处断言前先钉住前提——
第 1 轮必须真的产出 fills 且真的有票被定到正股数（见 `_run` 的 assert 与
`test_前提_第一轮必须真的动起来`）。变异实证：把 `_gap_fill_shares` 的收敛守卫改回
「比本轮计划额」，本文件必须变红。
"""
from __future__ import annotations

from bottleneck_hunter.watchlist.decision_engine import (
    _GAP_STEP_FRACTION,
    _gap_fill_shares,
    _plan_gap_fills,
)

# sideways/balanced：权益 40~60、现金上限 40（与 test_gap_driver 的 BOUNDS 同源）
BOUNDS = {"equity_min": 40, "equity_max": 60, "cash_max": 40}
# 报告 P1-B 的场景：L2 目标权益 60%（5 票 × 12%），账户现金 90%
TARGET_PCT = 12.0
CORE = [{"ticker": t, "target_weight_pct": TARGET_PCT} for t in ("AAA", "BBB", "CCC", "DDD", "EEE")]
CAP_PCT = 12.0
PRICE = 100.0
TOTAL_EQUITY = 100_000.0
CASH_START = 90_000.0
HELD_START = {"AAA": 10_000.0}


def _run(rounds: int = 20) -> dict:
    """跑真实路径的多轮闭环，返回逐轮轨迹与终态。"""
    account = {"total_equity": TOTAL_EQUITY, "cash_balance": CASH_START}
    held = dict(HELD_START)
    rounds_log: list[dict] = []
    for r in range(1, rounds + 1):
        positions = [{"ticker": t, "market_value": v} for t, v in held.items()]
        equity_pct_before = sum(held.values()) / TOTAL_EQUITY * 100
        fills = _plan_gap_fills(account, positions, BOUNDS, CORE, "us_stock")
        if not fills:
            rounds_log.append({"round": r, "fills": [], "deployed": 0.0, "sized": 0,
                               "equity_pct_before": equity_pct_before, "stopped": True})
            break
        deployed, sized, per_ticker = 0.0, 0, {}
        for f in fills:
            # 目标持仓额 = 该票计划自报的目标权重 × 权益（L4 侧同一口径，见调用点 _target_value）
            target_value = f["target_weight_pct"] / 100 * TOTAL_EQUITY
            shares = _gap_fill_shares(f["amount"], PRICE, TOTAL_EQUITY,
                                      held.get(f["ticker"], 0.0), CAP_PCT, "us_stock", target_value)
            per_ticker[f["ticker"]] = {"planned": f["amount"], "shares": shares}
            if shares <= 0:
                continue
            amount = shares * PRICE
            account["cash_balance"] -= amount
            held[f["ticker"]] = held.get(f["ticker"], 0.0) + amount
            deployed += amount
            sized += 1
        rounds_log.append({
            "round": r, "fills": fills, "deployed": deployed, "sized": sized,
            "equity_pct_before": equity_pct_before, "per_ticker": per_ticker,
            "equity_pct_after": sum(held.values()) / TOTAL_EQUITY * 100,
            "step_cap": (BOUNDS["equity_min"] - equity_pct_before) / 100 * TOTAL_EQUITY * _GAP_STEP_FRACTION,
        })
    return {"rounds": rounds_log, "held": held, "cash": account["cash_balance"],
            "equity_pct": sum(held.values()) / TOTAL_EQUITY * 100}


def test_前提_第一轮必须真的动起来():
    """前提断言：本场景第 1 轮必须有意图、且真有票被定到正股数。

    没有这一条，下面所有"逼近/收敛"的断言都可能在"循环体一次没进"的情况下恒真——
    这正是本仓库已复发三次的恒真夹具形态。
    """
    r1 = _run()["rounds"][0]
    assert r1["fills"], "前提不成立：缺口 30pct 却无意图，后面的收敛断言会全部恒真"
    assert r1["sized"] > 0, "前提不成立：第 1 轮没有任何一票被定到正股数"


def test_每轮都动得起来_不产出注定归零的空计划():
    """N-21 的直接症状：第 2 轮起意图仍在，却**每一票都定到 0 股** —— 全是废计划。

    修复前：第 1 轮 19.30%，第 2 轮起 fills 非空但 sized 恒为 0，权重原地不动。
    """
    out = _run()
    dead = [r for r in out["rounds"] if r["fills"] and r["sized"] == 0]
    assert not dead, f"第 {[r['round'] for r in dead]} 轮产出了全数归零的废计划（N-21 复发）"


def test_权益占比单调逼近并进入目标带():
    """判据 1：非递减、且**真的进了带**（equity_min=40），不是无限逼近。"""
    out = _run()
    traj = [r["equity_pct_before"] for r in out["rounds"]]
    assert traj == sorted(traj), f"权益占比必须单调不减（不得回退），实际轨迹 {traj}"
    assert out["equity_pct"] >= BOUNDS["equity_min"], (
        f"收敛判据未达成：终态权益 {out['equity_pct']:.2f}% 仍在目标带下限 "
        f"{BOUNDS['equity_min']}% 之下——驱动器永远追不到，就是 N-23 那种"
        f"「天天跑、组合纹丝不动」的复发。轨迹 {[round(t, 2) for t in traj]}"
    )
    assert out["equity_pct"] <= BOUNDS["equity_max"], "越过了权益上限（驱动器不该把组合顶穿上限）"


def test_停在带内后不再产生新计划():
    """判据 2：收敛即停 —— 停手后重跑不得再产意图（不刷单）。"""
    out = _run()
    assert out["rounds"][-1].get("stopped"), (
        f"跑了 {len(out['rounds'])} 轮仍未停手：{out['rounds'][-1].get('fills')}"
    )
    # 用终态账户重跑一轮：必须无意图
    positions = [{"ticker": t, "market_value": v} for t, v in out["held"].items()]
    again = _plan_gap_fills({"total_equity": TOTAL_EQUITY, "cash_balance": out["cash"]},
                            positions, BOUNDS, CORE, "us_stock")
    assert again == [], f"停在带内后仍产计划（刷单）：{again}"


def test_每轮总额不超步长上限():
    """判据 3：单轮部署额 ≤ 缺口 × 步长系数（防一次性梭哈）。

    收尾轮例外——步长已小到产不出一单时，驱动器改为把剩余缺口一次补满（否则数学上永远只能
    无限逼近 40% 而下限带进不去）。此时的安全不变量换成更强的那条：**不越过剩余缺口**，
    也就更不会越过目标带。两种情形都不允许出现"一口气把现金梭光"。
    """
    out = _run()
    frag = TOTAL_EQUITY * 0.005
    for r in out["rounds"]:
        if not r["fills"]:
            continue
        gap_dollars = (BOUNDS["equity_min"] - r["equity_pct_before"]) / 100 * TOTAL_EQUITY
        endgame = r["step_cap"] < frag or (gap_dollars - r["step_cap"]) < frag
        cap = gap_dollars if endgame else r["step_cap"]
        limit = "剩余缺口" if endgame else "步长上限"
        assert r["deployed"] <= cap + 1e-6, (
            f"第 {r['round']} 轮部署 {r['deployed']:.2f} 超过{limit} {cap:.2f}"
        )


def test_每票不越过L2目标权重():
    """补到目标即停：终态任一票都不得超过其 L2 目标权重（+0.5pct 容差，与 L3 的碎单阈值同源）。"""
    out = _run()
    for tk, v in out["held"].items():
        w = v / TOTAL_EQUITY * 100
        assert w <= TARGET_PCT + 0.5 + 1e-6, f"{tk} 补到 {w:.2f}%，越过 L2 目标 {TARGET_PCT}%"


def test_现金不为负():
    """不产生超出账户现金的补仓（deployable_cash 封顶）。"""
    out = _run()
    assert out["cash"] >= -1e-6, f"现金被补成负数 {out['cash']}"


if __name__ == "__main__":
    out = _run()
    print(f"{'轮':>3} {'轮初权益%':>10} {'部署':>9} {'定到股数(票数)':>14} {'步长上限':>9}")
    for r in out["rounds"]:
        print(f"{r['round']:>3} {r['equity_pct_before']:>10.2f} {r['deployed']:>9.0f} "
              f"{r['sized']:>14} {r.get('step_cap', 0):>9.0f}")
    print(f"终态：权益 {out['equity_pct']:.2f}%（目标带 {BOUNDS['equity_min']}~{BOUNDS['equity_max']}%），"
          f"现金 {out['cash']:.0f}")
    print({t: round(v / TOTAL_EQUITY * 100, 2) for t, v in sorted(out["held"].items())})
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("P1-B 缺口收敛自检通过")
