"""P0-E（L2 自洽告警）+ P1-I/N-25（驱动意图确定性直通）的生成期护栏。

两个缺陷的病历（见 docs/DECISION_CHAIN_REVIEW_2026-09-23.md）：
  · **N-23**：L2 顶层 `target_allocation.equity_pct=51`，但 core_holdings 四票合计只有 33%，
    相差 18pct。顶层那个数**没有任何下游消费者**（缺口驱动器/偏离报告/前端对照条全部只读明细），
    于是它写错时无人报错，却与 L1 下限一起制造一个够不着的缺口（恒 7pct、永不收敛）。
  · **N-25**：`driver_plans` 合并完就交回 LLM 去筛，能不能落地取决于 LLM 恰好同选。确定性驱动器
    的确定性止于"算金额"那一步 —— 实测缺口驱动 5 票里只有 2 票被同选，其余**无声消失**。

本文件钉住：告警按**三分法**口径（明细 vs equity_pct，不是明细+现金+对冲）触发/不触发，
以及驱动票在 LLM **完全不理**的情况下依然逐票进入待确认区或带原因的被拦截区。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from bottleneck_hunter.watchlist.decision_engine import (
    _allocation_inconsistency,
    _gap_driven_plan_details,
    _gap_driven_plans,
    run_execution_plans,
)
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.store_base import _today

UID = "u1"


# ───────────────────────── P0-E：L2 自洽判据（纯函数） ─────────────────────────

def _l2(equity_pct, holdings, cash_pct=25.0, hedge_pct=5.0):
    return {
        "target_allocation": {"equity_pct": equity_pct, "cash_pct": cash_pct, "hedge_pct": hedge_pct},
        "stock_selection": {"core_holdings": [{"ticker": t, "target_weight_pct": w} for t, w in holdings]},
    }


def test_健康的三分法不报警():
    """三分法：equity 70 = 明细 70（50+20），现金 25、对冲 5 另计。

    这里是最容易被写错的地方 —— 把现金/对冲也加进明细再去比 equity_pct，则每份**健康**的计划
    都相差 -30pct 而被误报（本仓库自家报告 P0-E 原文就带这个量纲滑动）。本用例是它的护栏。
    """
    assert _allocation_inconsistency(_l2(70.0, [("AAPL", 50.0), ("MSFT", 20.0)])) is None


def test_明细装不下承诺的权益_报警():
    """N-23 的生产形态：顶层 51% vs 明细 33% → 相差 18pct，必须点名。"""
    got = _allocation_inconsistency(
        _l2(51.0, [("JPM", 9.0), ("BAC", 8.0), ("META", 8.0), ("GOOGL", 8.0)], cash_pct=32.0, hedge_pct=17.0))

    assert got is not None
    assert got["equity_pct"] == 51.0
    assert got["detail_sum_pct"] == 33.0
    assert got["diff_pct"] == 18.0
    assert "18.0" in got["detail"]


def test_容差内的差异不报警():
    """LLM 的权重取整（0.5pct 级）不该天天刷告警——容差内视为自洽。"""
    assert _allocation_inconsistency(_l2(70.0, [("AAPL", 50.0), ("MSFT", 16.0)])) is None  # 差 4pct


def test_未选票时不报警():
    """空仓/仅观察期没有明细可对，不该报错（否则每轮都刷一条无意义的告警）。"""
    assert _allocation_inconsistency(_l2(70.0, [])) is None
    assert _allocation_inconsistency({"target_allocation": {"equity_pct": 70}}) is None
    assert _allocation_inconsistency(None) is None


# ───────────────── P1-I/N-25：驱动票不经 LLM 取舍也照样落地 ─────────────────

def test_驱动计划明细带回目标水位():
    """L4 定股要判"离 L2 目标还有多远"，而目标权重只在 L3 的计划里 → 必须能读回来。"""
    plans = [
        {"ticker": "NVDA", "market": "us_stock",
         "result_json": {"gap_driven": True, "_planned_amount": 900.0, "target_weight_pct": 9.0}},
        {"ticker": "AMD", "market": "us_stock",
         "result_json": {"opportunity_driven": True, "_planned_amount": 500.0, "target_weight_pct": 6.0}},
        {"ticker": "MSFT", "market": "us_stock", "result_json": {"action": "buy"}},   # LLM 择时，无标记
        {"ticker": "600519.SS", "market": "a_stock",
         "result_json": {"gap_driven": True, "_planned_amount": 700.0, "target_weight_pct": 12.0}},
    ]

    got = _gap_driven_plan_details(plans, "us_stock")

    assert set(got) == {"NVDA", "AMD"}
    assert got["NVDA"] == {"amount": 900.0, "target_pct": 9.0, "opportunity": False}
    assert got["AMD"]["opportunity"] is True
    # 与粗判 map 必须同源同判据，否则"豁免放行"与"定股水位"会各看各的集合
    assert set(got) == set(_gap_driven_plans(plans, "us_stock"))


class _DriverStore:
    """真 Store + 预置：L2 目标（NVDA 9%）、缺口驱动计划（NVDA）、账户全现金、有最新收盘价。"""

    def __init__(self, tmp_path, nvda_plan=True, llm_ignores_driver=True):
        self.store = WatchlistStore(db_path=tmp_path / "drv.db", user_id=UID).for_user(UID).for_market("us_stock")
        s = self.store
        mid = s.create_macro_strategy(
            {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False)
        sid = s.create_strategic_plan(mid, {
            "stock_selection": {"core_holdings": [{"ticker": "NVDA", "target_weight_pct": 9.0}]},
            "target_allocation": {"equity_pct": 55, "cash_pct": 40, "hedge_pct": 5},
        }, strict=False)
        eid = s.add({"ticker": "NVDA", "company_name": "NVDA", "market": "us_stock",
                     "sector": "科技", "tier": "focus"})
        if nvda_plan:
            s.create_tactical_plan(sid, eid, "NVDA", _today(), {
                "action": "buy", "gap_driven": True, "_planned_amount": 900.0, "target_weight_pct": 9.0,
                "entry_plan": {}, "exit_plan": {}, "reasoning": "缺口驱动"}, strict=False)
        # 峰值/初始资金必须给足：默认 initial_capital 与 10 万权益对不上会被判「距峰值回撤 90%」
        # → 熔断 → 驱动单被拦，本文件的断言全部恒真（这正是本仓库复发三次的恒真夹具形态）。
        s.update_sim_account(total_equity=100_000, current_capital=100_000, cash_balance=95_000,
                             peak_equity=100_000, initial_capital=100_000)
        s.save_snapshots([{"ticker": "NVDA", "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                           "close": 100.0}])
        self.llm_ignores_driver = llm_ignores_driver

    def plans(self):
        """LLM 只肯买 MSFT —— 对 NVDA 完全不理会（正是 N-25 的病史场景）。

        `llm_echoes_driver=True` 时改为**同选** NVDA：这条路径与合成路径走的是不同的代码分支
        （合成因 `_exec_tk` 让位，改由普通循环的"已有 pending"那道门把关），必须分别测。
        """
        if getattr(self, "llm_echoes_driver", False):
            return {"execution_plans": [{"ticker": "NVDA", "action": "buy", "shares": 5,
                                         "target_price": 100.0, "confidence": 8, "reasoning": "LLM 同选"}],
                    "execution_summary": {}, "skipped_plans": []}
        if self.llm_ignores_driver:
            return {"execution_plans": [{"ticker": "MSFT", "action": "buy", "shares": 100,
                                         "target_price": 100.0, "confidence": 8, "reasoning": "t"}],
                    "execution_summary": {}, "skipped_plans": []}
        return {"execution_plans": [], "execution_summary": {}, "skipped_plans": []}


def _run(store, plans):
    llm = MagicMock()
    llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps(plans)))

    async def _negotiate(_llm, _prompt, **_kw):
        return plans, []

    async def _collect(gen):
        return [e async for e in gen]

    with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
               return_value=(llm, "stub", "stub")), \
         patch("bottleneck_hunter.watchlist.decision_engine._run_data_negotiation", _negotiate):
        return asyncio.run(_collect(run_execution_plans(store)))


def test_LLM不理会的驱动票仍然落地(tmp_path):
    """N-25 的核心断言：LLM 一份计划都不给 NVDA，它也必须出现在待确认区。

    修复前：驱动意图只在 LLM 恰好同选时才落地，其余**无声消失**（计划还留在库里，所以没人看得见）。
    """
    d = _DriverStore(tmp_path)
    _run(d.store, d.plans())

    by_tk = {p["ticker"]: p for p in d.store.get_pending_executions()}
    # LLM 自选的 MSFT 照旧落地（本修复不夺它的权），关键是 NVDA **也**在 —— 不靠 LLM 点头
    assert "NVDA" in by_tk, (
        f"LLM 没选 NVDA，驱动票就被无声丢弃了（N-25 复发）：实际 {sorted(by_tk)}"
    )
    assert by_tk["NVDA"]["shares"] == 9   # min(本轮计划 900, 距 L2 目标 9%×10万=9000) / 100
    assert by_tk["NVDA"]["action"] == "buy"


def test_无最新收盘价时驱动票不合成_但不无声(tmp_path):
    """定不出股就不落库（不写"--股"的不可执行指令），且不许无声吞掉 —— 日志必须留痕。

    无价的票若被合成，`_gap_fill_shares` 会因 price<=0 返 0 → 走 skipped 分支，用户仍然
    看不到任何痕迹；所以在合成前就挡掉并记账。
    """
    d = _DriverStore(tmp_path)
    with d.store._connect() as conn:
        conn.execute("DELETE FROM market_snapshots")
        conn.commit()
    logs = []
    with patch("bottleneck_hunter.watchlist.decision_engine.logger") as lg:
        lg.info.side_effect = lambda *a, **k: logs.append(a[0] % (a[1:] or ()))
        _run(d.store, {"execution_plans": [], "execution_summary": {}, "skipped_plans": []})

    assert d.store.get_pending_executions() == []
    assert any("无最新收盘价" in m for m in logs), f"驱动票被无声丢弃，日志里没有原因：{logs}"


def test_机会驱动的越线标记在合成后仍然保留(tmp_path):
    """N-25 的合成**不得**洗掉 `mandate_exception`：那是 auto_execute 摘出机会驱动单的唯一依据。

    洗掉的后果不是"少一条单"，而是越线单被当成普通单自动执行 —— 绕过强制人工确认。
    """
    d = _DriverStore(tmp_path, nvda_plan=False)
    s = d.store
    sid = s.get_latest_strategic_plan()["id"]
    eid = s.list_all()[0]["id"]
    s.create_tactical_plan(sid, eid, "NVDA", _today(), {
        "action": "add", "opportunity_driven": True, "mandate_exception": True,
        "_planned_amount": 900.0, "target_weight_pct": 6.0}, strict=False)
    # 无持仓 → 「执行后权重 > L2 目标」的越线判定必然成立（L2 只承诺了 9%，档位上限 6%）

    _run(s, {"execution_plans": [], "execution_summary": {}, "skipped_plans": []})

    pending = s.get_pending_executions()
    assert len(pending) == 1 and pending[0]["ticker"] == "NVDA"
    from bottleneck_hunter.watchlist.auto_execute import _is_mandate_exception
    assert _is_mandate_exception(pending[0]) is True, (
        "合成路径洗掉了 mandate_exception → 越线单会被自动执行，绕过人工确认"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


# ─────────────── P1-H：缺口驱动崩了，不许把 LLM 那批计划一起带崩 ───────────────

def test_缺口驱动抛异常时L3照样交出LLM的计划(tmp_path):
    """P1-H 的核心断言。`run_tactical_plans` 的外层 `except` 包住整个 L3 体——缺口驱动一抛，
    L3 直接走 `decision_error` 返回：**LLM 自己算出来的那批战术计划也跟着没了**（它们已经在
    本轮被写进库，但事件流停在错误上，调用方/前端拿不到 decision_done，后续步骤全部不跑）。

    两个同级驱动器此前失败模式不一致：机会驱动有 try 包裹、缺口驱动没有。本用例制造一个
    **只可能崩在缺口驱动里**的畸形（L2 目标权重写成 "12%"）——`_plan_gap_fills` 实探确认
    抛 ValueError——然后断言 L3 仍然正常收尾。
    """
    from bottleneck_hunter.watchlist import decision_engine as de

    s = WatchlistStore(db_path=tmp_path / "l3.db", user_id=UID).for_user(UID).for_market("us_stock")
    mid = s.create_macro_strategy(
        {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False)
    # 畸形就在这里：tw 是字符串。LLM 偶发这般输出，且 L2 落库时不做类型强校验。
    s.create_strategic_plan(mid, {
        "stock_selection": {"core_holdings": [{"ticker": "NVDA", "target_weight_pct": "12%"}]},
        "target_allocation": {"equity_pct": 60, "cash_pct": 35, "hedge_pct": 5},
    }, strict=False)
    s.add({"ticker": "NVDA", "company_name": "NVDA", "market": "us_stock", "sector": "科技", "tier": "focus"})
    s.update_sim_account(total_equity=100_000, current_capital=100_000, cash_balance=95_000,
                         peak_equity=100_000, initial_capital=100_000)

    llm = MagicMock()
    llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps(
        {"tactical_plans": [{"ticker": "MSFT", "action": "buy", "urgency": "this_week",
                             "reasoning": "LLM 自己的择时"}]})))

    async def _negotiate(_llm, _prompt, **_kw):
        return {"tactical_plans": [{"ticker": "MSFT", "action": "buy", "urgency": "this_week",
                                    "reasoning": "LLM 自己的择时"}]}, []

    async def _collect(gen):
        return [e async for e in gen]

    with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
               return_value=(llm, "stub", "stub")), \
         patch("bottleneck_hunter.watchlist.decision_engine._run_data_negotiation", _negotiate):
        events = asyncio.run(_collect(de.run_tactical_plans(s)))

    kinds = [e["event"] for e in events]
    assert "decision_error" not in kinds, (
        f"缺口驱动一崩，整层 L3 就中止了（P1-H 未生效）：{kinds} "
        f"{[e for e in events if e['event'] == 'decision_error']}"
    )
    done = next(e for e in events if e["event"] == "decision_done")["data"]
    assert done["gap_driven_count"] == 0, "缺口驱动应失败降级为 0，而不是产出半截"
    # LLM 的计划照旧落地——这才是这层兜底真正保住的东西
    assert {r["ticker"] for r in s.get_tactical_plans_by_date()} == {"MSFT"}


def test_缺口驱动失败要留痕不能静默(tmp_path):
    """兜底不等于静默：降级必须进日志，否则「驱动器悄悄不干活」和 N-25 是同一种病。"""
    from bottleneck_hunter.watchlist import decision_engine as de

    s = WatchlistStore(db_path=tmp_path / "l3b.db", user_id=UID).for_user(UID).for_market("us_stock")
    mid = s.create_macro_strategy(
        {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False)
    s.create_strategic_plan(mid, {
        "stock_selection": {"core_holdings": [{"ticker": "NVDA", "target_weight_pct": "12%"}]},
        "target_allocation": {"equity_pct": 60, "cash_pct": 35, "hedge_pct": 5},
    }, strict=False)
    s.add({"ticker": "NVDA", "company_name": "NVDA", "market": "us_stock", "sector": "科技", "tier": "focus"})
    s.update_sim_account(total_equity=100_000, current_capital=100_000, cash_balance=95_000,
                         peak_equity=100_000, initial_capital=100_000)

    llm = MagicMock()
    llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps({"tactical_plans": []})))

    async def _negotiate(_llm, _prompt, **_kw):
        return {"tactical_plans": []}, []

    async def _collect(gen):
        return [e async for e in gen]

    logs = []
    with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
               return_value=(llm, "stub", "stub")), \
         patch("bottleneck_hunter.watchlist.decision_engine._run_data_negotiation", _negotiate), \
         patch("bottleneck_hunter.watchlist.decision_engine.logger") as lg:
        lg.warning.side_effect = lambda *a, **k: logs.append(a[0] % (a[1:] or ()))
        lg.info.side_effect = lambda *a, **k: None
        lg.exception.side_effect = lambda *a, **k: None
        asyncio.run(_collect(de.run_tactical_plans(s)))

    assert any("缺口驱动失败" in m for m in logs), f"缺口驱动静默降级了：{logs}"


def test_机会驱动的水位取档位上限而非L2承诺(tmp_path):
    """L4 调用点的分支：两条驱动的「目标」不是一回事，取错就把机会驱动关掉了。

    机会驱动的存在意义就是**越过 L2 承诺**（P0-4）。L2 只承诺 NVDA 9%，而档位允许追到 12%，
    已持 10%（10000 元）——距 L2 承诺只剩 -1%（早该收手），距档位上限还有 2000 元。
    水位若取 L2 承诺 → 恒返 0（越线永久失效）；取档位上限 → 补 20 股。
    """
    s = WatchlistStore(db_path=tmp_path / "opp.db", user_id=UID).for_user(UID).for_market("us_stock")
    mid = s.create_macro_strategy(
        {"regime": "sideways", "risk_appetite": "balanced", "regime_confidence": 5}, strict=False)
    sid = s.create_strategic_plan(mid, {
        "stock_selection": {"core_holdings": [{"ticker": "NVDA", "target_weight_pct": 9.0}]},
        "target_allocation": {"equity_pct": 55, "cash_pct": 40, "hedge_pct": 5},
    }, strict=False)
    eid = s.add({"ticker": "NVDA", "company_name": "NVDA", "market": "us_stock",
                 "sector": "科技", "tier": "focus"})
    # 机会驱动计划：档位 12%（越线），本轮金额 2000，已持 10% → 只有按档位取水位才补得动
    s.create_tactical_plan(sid, eid, "NVDA", _today(), {
        "action": "add", "opportunity_driven": True, "mandate_exception": True,
        "_planned_amount": 2000.0, "target_weight_pct": 12.0, "entry_plan": {}, "exit_plan": {},
        "reasoning": "机会驱动"}, strict=False)
    acct = s.get_sim_account()
    s.create_sim_position(acct["id"], "NVDA", shares=100, avg_cost=100.0)
    s.update_sim_position(s.get_sim_position(acct["id"], "NVDA")["id"], current_price=100.0,
                          market_value=10_000.0)
    s.update_sim_account(total_equity=100_000, current_capital=100_000, cash_balance=30_000,
                         peak_equity=100_000, initial_capital=100_000)
    s.save_snapshots([{"ticker": "NVDA", "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                       "close": 100.0}])

    llm = MagicMock()
    llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps(
        {"execution_plans": [], "execution_summary": {}, "skipped_plans": []})))

    async def _negotiate(_llm, _prompt, **_kw):
        return {"execution_plans": [], "execution_summary": {}, "skipped_plans": []}, []

    async def _collect(gen):
        return [e async for e in gen]

    with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
               return_value=(llm, "stub", "stub")), \
         patch("bottleneck_hunter.watchlist.decision_engine._run_data_negotiation", _negotiate):
        asyncio.run(_collect(run_execution_plans(s)))

    pending = {p["ticker"]: p for p in s.get_pending_executions()}
    assert "NVDA" in pending, "机会驱动的越线意图被 L2 承诺的水位掐死了（水位取错分支）"
    assert pending["NVDA"]["shares"] == 20, f"应补到档位 12%（还差 2000 元 = 20 股），实际 {pending['NVDA']['shares']}"


# ───────── N-32：两驱动器同票时预算取大（不得让后合并的一方静默蒸发） ─────────

def test_两驱动器同票时取大而非覆盖():
    """N-32 的核心断言：`{**gap_plans, **opp_plans}` 的纯覆盖会让一方预算凭空消失。

    顺序无关同样要钉——"谁赢"必须由金额决定，不能由 dict 字面量的书写顺序决定。
    """
    plans = [
        {"ticker": "NVDA", "market": "us_stock",
         "result_json": {"gap_driven": True, "_planned_amount": 900.0, "target_weight_pct": 9.0}},
        {"ticker": "NVDA", "market": "us_stock",
         "result_json": {"opportunity_driven": True, "_planned_amount": 2100.0, "target_weight_pct": 6.0}},
    ]
    got = _gap_driven_plan_details(plans, "us_stock")["NVDA"]
    assert got["amount"] == 2100.0, f"预算被覆盖而非取大：{got}"
    assert got["target_pct"] == 9.0, f"水位取小会掐死缺口侧：{got}"
    # 越线标记取或：多问一次人，好过越线单被当普通单静默成交
    assert got["opportunity"] is True, f"机会驱动标记被缺口那张盖掉了：{got}"
    # 顺序无关
    assert _gap_driven_plan_details(list(reversed(plans)), "us_stock")["NVDA"] == got


# ───────── 驱动票不得逐轮叠 pending 卡 ─────────

def _stack_rounds(tmp_path, rounds=3, echo=False):
    """连跑 N 轮 L4，返回每轮结束后的同票 pending 卡数。"""
    d = _DriverStore(tmp_path)
    d.llm_echoes_driver = echo
    counts = []
    for _ in range(rounds):
        _run(d.store, d.plans())
        counts.append(len([p for p in d.store.get_pending_executions() if p["ticker"] == "NVDA"]))
    return d, counts


def test_驱动票不逐轮叠pending卡(tmp_path):
    """每轮重跑不许再叠一张：pending 里那张还没处理，就代表"这票还没完"。

    修复前实测第 1/2/3 轮 = 1/2/3 张（`existing_tickers` 那道门对 driver 票方向盲地豁免）。
    危害在消费端：`auto_execute_pending` 逐条成交、不按 ticker 去重，用户一次全选就把
    单轮步长上限击穿，而 pending 老化有 14 天。
    """
    d, counts = _stack_rounds(tmp_path)
    assert counts[0] == 1, f"前提不成立：第一轮就该有 1 张卡，实际 {counts}"
    assert counts == [1, 1, 1], f"驱动票逐轮叠卡（N-32 之外的既有缺陷复发）：{counts}"


def test_驱动票的旧卡处理掉之后还能再补(tmp_path):
    """收窄豁免不得变成"这票永不再买"——旧的卡一旦离开 pending，下一轮必须能重新生成。"""
    d = _DriverStore(tmp_path)
    _run(d.store, d.plans())
    # 按 ticker 取，不能用 [0]：LLM 还会给一张 MSFT，同优先级下顺序不稳
    first = next(p for p in d.store.get_pending_executions() if p["ticker"] == "NVDA")
    with d.store._connect() as conn:
        conn.execute("UPDATE execution_plans SET status='executed' WHERE id=?", (first["id"],))
        conn.commit()
    assert not [p for p in d.store.get_pending_executions() if p["ticker"] == "NVDA"], (
        "前提不成立：旧卡没转走"
    )

    _run(d.store, d.plans())  # 重跑一轮

    assert [p["ticker"] for p in d.store.get_pending_executions()].count("NVDA") == 1, (
        "旧卡转走后仍不再生成——豁免被收得太紧，驱动被永久封死"
    )


def test_LLM同选驱动票时也不叠pending卡(tmp_path):
    """与上一条互补：**合成路径被 `_exec_tk` 让位**时（LLM 自己选了这张驱动票），
    票改走普通循环，由"已有 pending"那道门把关。两条路径的守卫不同，必须各钉一条——
    实测过：只钉合成路径时，把豁免改回方向盲的原样（变异测试）本文件仍全绿。
    """
    d, counts = _stack_rounds(tmp_path, rounds=3, echo=True)
    assert counts[0] == 1, f"前提不成立：LLM 同选时第一轮该有 1 张卡，实际 {counts}"
    assert counts == [1, 1, 1], f"LLM 同选路径逐轮叠卡：{counts}"
