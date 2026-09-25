"""P0-D（N-22）当日累计换手卡口 回归测试。

病史：`validate_execution_plan` 的日额度判据是「**单笔**金额 vs 当日总额度」，且从不查当日
已成交额。日换手是**累加量**，逐笔比对在数学上不可能守住——实测 10 笔各 5% 权益全部放行，
单日累计 50% 权益 vs 上限 30%（拆得越碎越没上限）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bottleneck_hunter.watchlist.constraint_validator import (
    DEFAULT_CONSTRAINTS,
    max_compliant_shares,
    validate_batch,
    validate_execution_plan,
)
from bottleneck_hunter.watchlist.store import WatchlistStore

UID = "u_turn"
# 权益 1,000,000、日额度 30% = 300,000、单笔 50% = 500,000、现金下限 15% = 150,000
ACCOUNT = {"id": "acc1", "cash_balance": 1_000_000, "total_equity": 1_000_000,
           "initial_capital": 1_000_000}
C = {**DEFAULT_CONSTRAINTS}


def _plan(shares, price=100.0, action="buy", ticker="AAA"):
    return {"id": f"p_{ticker}", "action": action, "ticker": ticker, "shares": shares,
            "target_price": price, "result_json": {"action": action, "shares": shares,
                                                   "target_price": price, "market": "us_stock"}}


class TestDailyTurnoverGate:
    def test_单笔超额度仍拦(self):
        """旧行为不得回退：单笔 350,000 > 日额度 300,000。"""
        v = validate_execution_plan(_plan(3500), ACCOUNT, [], C)
        assert not v.valid
        assert any("日交易额度" in s for s in v.violations)

    def test_单笔不超但累计超则拦(self):
        """本缺陷的判据：单笔 50,000 各看都不超，累计 300,000 之后第 7 笔必须拦。"""
        v = validate_execution_plan(_plan(500), ACCOUNT, [], C, daily_turnover_used=280_000)
        assert not v.valid
        assert any("叠加当日已成交" in s for s in v.violations)

    def test_累计未超则放行且报剩余额度(self):
        v = validate_execution_plan(_plan(500), ACCOUNT, [], C, daily_turnover_used=100_000)
        assert v.valid, v.violations

    def test_恰好等于额度放行(self):
        """边界：已用 250,000 + 本笔 50,000 = 300,000，不超即放行。"""
        assert validate_execution_plan(_plan(500), ACCOUNT, [], C,
                                       daily_turnover_used=250_000).valid

    def test_默认零保持旧调用方行为(self):
        """不传日额度 = 无从得知已成交额 → 按单笔口径（宁可放行，不误拦真实委托）。"""
        assert validate_execution_plan(_plan(500), ACCOUNT, [], C).valid

    def test_卖单也受日额度约束(self):
        """换手是双向的：卖出同样消耗当日额度。

        病史：原用例传 positions=[]，于是计划在「无持仓」那条硬校验里就 return 了，
        第 5 段（日额度）**一次都没跑过** —— `not v.valid` 恒真，把 used 换成任何值都绿。
        故这里必须给足持仓，让计划活着走到日额度那一段。
        """
        positions = [{"ticker": "AAA", "shares": 1000, "market_value": 100_000}]
        # 前提：不给已用额度时该卖单完全合规（证明下面的判红确实来自日额度那一段）
        assert validate_execution_plan(_plan(500, action="reduce"), ACCOUNT, positions, C).valid
        v = validate_execution_plan(_plan(500, action="reduce"), ACCOUNT, positions,
                                    C, daily_turnover_used=290_000)
        assert any("叠加当日已成交" in s for s in v.violations)

    def test_批量校验必须传递日额度(self):
        """validate_batch 不传日额度等于整批跳过卡口 —— 这正是批量下单的真实场景。"""
        plans = [_plan(500, ticker=f"T{i}") for i in range(3)]
        ok = validate_batch(plans, ACCOUNT, [], C, daily_turnover_used=260_000)
        assert all(not r.valid for r in ok.values()), "已用 260,000 + 三笔各 50,000 必超"
        assert all(any("日交易额度" in s for s in r.violations) for r in ok.values())

    def test_批量校验在批内累积额度(self):
        """批内也要累加：3 笔各 50,000、已用 250,000 → 第 1 笔正好顶到 300,000 上限，后两笔必拦。

        逐笔独立校验（不累积）会让三笔全过 → 单批放出 150,000 而非剩余 50,000。
        """
        plans = [_plan(500, ticker=f"T{i}") for i in range(3)]
        ok = validate_batch(plans, ACCOUNT, [], C, daily_turnover_used=250_000)
        assert ok["p_T0"].valid, "第 1 笔 250,000+50,000=300,000 恰好用满，应放行"
        assert not ok["p_T1"].valid and not ok["p_T2"].valid, "额度已用尽，后两笔必须拦"


class TestMaxCompliantSharesUsesRemainingBudget:
    def test_日额度按剩余缩量(self):
        """已用 280,000、本笔想买 5,000 股@100 → 只剩 20,000 额度 → 缩到 200 股。"""
        n = max_compliant_shares(_plan(5000), ACCOUNT, [], C, daily_turnover_used=280_000)
        assert n == 200

    def test_额度用尽缩到零(self):
        assert max_compliant_shares(_plan(5000), ACCOUNT, [], C, daily_turnover_used=300_000) == 0

    def test_现金下限按账户现金缩量(self):
        """生成期用影子账本余额当 account 现金：剩 200,000、下限 150,000 → 最多买 500 股。"""
        shadow = {**ACCOUNT, "cash_balance": 200_000}
        assert max_compliant_shares(_plan(5000), shadow, [], C) == 500

    def test_不传则沿用账户现金(self):
        """不传影子余额：现金下限 850,000 不设限，改由单股占比 25% = 250,000 → 2,500 股绑定。"""
        assert max_compliant_shares(_plan(5000), ACCOUNT, [], C) == 2500


class TestShadowLedger:
    """报告验证案 1：5×100,000 / 权益 1,000,000 / 现金 400,000 / 下限 15%。

    逐笔影子账本 → 第 1、2 条原样通过，第 3 条缩到 50,000，第 4、5 条缩到 0。
    """

    def _simulate(self, cash_start=400_000.0, n_plans=5, each=1000, price=100.0):
        """模拟生成期循环：影子现金 + 当日累计额度，两者都随已放行计划递减。"""
        account = {**ACCOUNT, "cash_balance": cash_start}
        cash_left, turnover = cash_start, 0.0
        kept = []
        for i in range(n_plans):
            ep = _plan(each, price, ticker=f"T{i}")
            view = {**account, "cash_balance": cash_left}   # 与生成期同构：校验看影子余额
            vr = validate_execution_plan(ep, view, [], C, daily_turnover_used=turnover)
            if not vr.valid:
                shr = max_compliant_shares(ep, view, [], C, daily_turnover_used=turnover)
                if shr <= 0:
                    continue                      # 缩不到 1 手 → 静默放弃，不留注定失败的单
                ep["shares"], ep["auto_adjusted"] = shr, True
            amt = ep["shares"] * price
            cash_left -= amt
            turnover += amt
            kept.append((ep["ticker"], ep["shares"], amt))
        return kept, cash_left, turnover

    def test_现金随前序计划递减(self):
        kept, cash_left, _ = self._simulate()
        assert [(t, s) for t, s, _ in kept] == [("T0", 1000), ("T1", 1000), ("T2", 500)]
        assert cash_left == pytest.approx(150_000)      # 恰好停在 15% 下限
        assert len(kept) == 3, "第 4、5 条缩到 0 应静默放弃，不再产出注定失败的待确认单"

    def test_修复前必红的对照(self):
        """旧行为：每笔都按「买入前满额现金」校验 → 5 条全部落成待确认单。"""
        account = {**ACCOUNT, "cash_balance": 400_000}
        passed = [i for i in range(5)
                  if validate_execution_plan(_plan(1000, ticker=f"T{i}"), account, [], C).valid]
        assert passed == [0, 1, 2, 3, 4]                # 现状 5 条全过
        kept, _, _ = self._simulate()
        assert len(kept) < 5                            # 装卡口后收紧

    def test_换手侧累计不超上限(self):
        """报告验证案 2：10 条各 50,000 → 累计成交额必须 ≤ 300,000（修复前 500,000）。"""
        account = {**ACCOUNT, "cash_balance": 1_000_000}
        turnover, kept = 0.0, 0
        for i in range(10):
            ep = _plan(500, ticker=f"T{i}")
            vr = validate_execution_plan(ep, account, [], C, daily_turnover_used=turnover)
            if not vr.valid:
                shr = max_compliant_shares(ep, account, [], C, daily_turnover_used=turnover)
                if shr <= 0:
                    continue
                ep["shares"] = shr
            turnover += ep["shares"] * 100.0
            kept += 1
        assert turnover <= 300_000, f"累计换手 {turnover} 超过日额度"
        assert kept == 6 and turnover == 300_000        # 6×50,000 用满，后 4 条缩到 0

    def test_预置已成交额后只放行剩余(self):
        """报告验证案 2 后半：预置当日已成交 280,000 → 本轮只放行 20,000。"""
        account = {**ACCOUNT, "cash_balance": 1_000_000}
        ep = _plan(500, ticker="T0")                     # 想买 50,000
        assert not validate_execution_plan(ep, account, [], C, daily_turnover_used=280_000).valid
        assert max_compliant_shares(ep, account, [], C, daily_turnover_used=280_000) == 200  # 20,000


class TestDailyTurnoverReader:
    """store 侧按「北京当日」汇总已成交额（created_at 是 UTC）。"""

    @pytest.fixture
    def store(self, tmp_path):
        return WatchlistStore(db_path=tmp_path / "t.db", user_id=UID).for_user(UID).for_market("us_stock")

    def _trade(self, store, amount, created_at, account_id="acc1", market="us_stock"):
        with store._write_conn() as conn:
            conn.execute(
                "INSERT INTO sim_trades (id, account_id, ticker, side, shares, price, amount, created_at, "
                "market, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"t{created_at}{amount}", account_id, "AAA", "buy", 1, amount, amount,
                 created_at, market, UID),
            )

    @staticmethod
    def _bj(hours_ago=0):
        """构造「北京当日某时刻」对应的 UTC 时间戳（当前北京 00:05 时，1 小时前属昨日）。"""
        bj = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(hours=hours_ago)
        return bj.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _bj_days_ago(days=1):
        """北京「前 N 日」同一时刻 —— 必落在北京昨日及更早。"""
        bj = datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=days)
        return bj.astimezone(timezone.utc).isoformat(timespec="seconds")

    def test_当日成交被汇总(self, store):
        self._trade(store, 50_000, self._bj(0))
        self._trade(store, 30_000, self._bj(0))
        assert store.daily_turnover_amount("acc1", "us_stock") == 80_000

    def test_跨日成交不计入(self, store):
        self._trade(store, 50_000, self._bj(0))
        self._trade(store, 999_999, self._bj_days_ago(1))
        assert store.daily_turnover_amount("acc1", "us_stock") == 50_000

    def test_按北京当日而非UTC当日(self, store):
        """北京 07:30 的成交：此刻 UTC 若还在前一日，按 UTC 归日就会漏计。"""
        bj = datetime.now(ZoneInfo("Asia/Shanghai"))
        bj_0730 = bj.replace(hour=7, minute=30, second=0, microsecond=0)
        if bj < bj_0730:                       # 北京还没到 07:30 → 该时刻属北京昨日
            pytest.skip("北京当前早于 07:30，本用例的构造前提不成立")
        stamp = bj_0730.astimezone(timezone.utc).isoformat(timespec="seconds")
        if stamp[:10] == bj.strftime("%Y-%m-%d"):
            pytest.skip("UTC 与北京同一日期，构造不出「UTC 昨日 / 北京当日」")
        self._trade(store, 123_456, stamp)
        assert store.daily_turnover_amount("acc1", "us_stock") == 123_456, (
            f"北京当日 07:30（UTC {stamp}）必须计入北京当日")

    def test_跨北京午夜归日(self, store):
        """北京 00:30 = UTC 前一日 16:30 —— 必须计入北京当日，而非 UTC 那天。"""
        bj = datetime.now(ZoneInfo("Asia/Shanghai"))
        bj_0030 = bj.replace(hour=0, minute=30, second=0, microsecond=0)
        if bj < bj_0030:
            pytest.skip("北京当前早于 00:30")
        stamp = bj_0030.astimezone(timezone.utc).isoformat(timespec="seconds")
        assert stamp[:10] != bj.strftime("%Y-%m-%d"), "构造前提：该时刻的 UTC 日期须为前一日"
        self._trade(store, 77_000, stamp)
        assert store.daily_turnover_amount("acc1", "us_stock") == 77_000

    def test_按账户与市场隔离(self, store):
        self._trade(store, 50_000, self._bj(0), account_id="acc1")
        self._trade(store, 70_000, self._bj(0), account_id="acc2")
        self._trade(store, 90_000, self._bj(0), account_id="acc1", market="a_stock")
        assert store.daily_turnover_amount("acc1", "us_stock") == 50_000
        assert store.daily_turnover_amount("acc2", "us_stock") == 70_000
        # for_market("us_stock") 的 store 已按市场过滤，故不传 market 也只看本市场
        assert store.daily_turnover_amount("acc1") == 50_000

    def test_空库返回零(self, store):
        assert store.daily_turnover_amount("acc1", "us_stock") == 0.0



class TestGenerationPhaseConsumesTurnover:
    """P0-D 端到端护栏：**生成期**（run_execution_plans）必须真把「当日已成交额」接进卡口。

    病史（变异测试实证）：把 decision_engine 里两处 `daily_turnover_used=` 摘掉、或把
    「本批已放行金额并入已用额度」那行摘掉，全量 2269 条测试**无一报警** —— 上面那些用例
    全是直接调 validate_execution_plan / max_compliant_shares 的单元级护栏，只证明「卡口本身
    算得对」，没证明「L4 生成期真的问了它」。这一环是整条修复的命门：参数不在生产路径上，
    日额度卡口就是个装饰品（N-22 原样复发，且无人能发现）。
    """

    def _store(self, tmp_path):
        from bottleneck_hunter.watchlist.store_base import _today

        s = WatchlistStore(db_path=tmp_path / "gen.db", user_id=UID).for_user(UID).for_market("us_stock")
        mid = s.create_macro_strategy(
            {"risk_appetite": "balanced", "regime": "sideways", "regime_confidence": 5}, strict=False)
        sid = s.create_strategic_plan(mid, {"target_allocation": [
            {"ticker": "AAPL", "weight": 0.06, "action": "buy"},
            {"ticker": "NVDA", "weight": 0.06, "action": "buy"}], "cash_reserve": 0.3}, strict=False)
        for tk in ("AAPL", "NVDA"):
            eid = s.add({"ticker": tk, "company_name": tk, "market": "us_stock",
                         "sector": "科技", "tier": "focus"})
            s.create_tactical_plan(sid, eid, tk, _today(), {
                "action": "buy", "confidence": 8, "entry_plan": {"price": 100.0},
                "exit_plan": {"stop_loss": 80.0}}, strict=False)
        # 账户 1,000,000 全现金 → 日额度 30% = 300,000；当日已成交 280,000 → 只剩 20,000
        acct = s.get_sim_account()
        s.create_sim_trade(acct["id"], "MSFT", "buy", 2800, 100.0, 280_000.0, strict=False)
        return s

    @staticmethod
    def _plans(shares):
        return {"execution_plans": [
            {"ticker": tk, "action": "buy", "shares": shares, "target_price": 100.0,
             "amount": shares * 100.0, "confidence": 8, "reasoning": "测试"} for tk in ("AAPL", "NVDA")
        ], "execution_summary": {}, "skipped_plans": []}

    def _run(self, store, shares=5000):
        import asyncio as _asyncio
        from unittest.mock import MagicMock, patch

        from bottleneck_hunter.watchlist.decision_engine import run_execution_plans

        llm = MagicMock()   # 任何 invoke（含 P0.2 自修正）都返回同一份计划：修不了，只能走降级缩量
        llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps(self._plans(shares))))

        async def _negotiate(_llm, _prompt, **_kw):
            return self._plans(shares), []

        with patch("bottleneck_hunter.watchlist.decision_engine.get_llm_for_position",
                   return_value=(llm, "stub", "stub")), \
             patch("bottleneck_hunter.watchlist.decision_engine._run_data_negotiation", _negotiate):
            return _asyncio.run(self._collect(run_execution_plans(store))), store.get_pending_executions()

    @staticmethod
    async def _collect(gen):
        return [e async for e in gen]

    def test_生成期按当日已成交后的剩余额度缩量(self, tmp_path):
        """两条各想买 1200 股（12% 单股上限、各 120,000），但日额度只剩 20,000（200 股）。"""
        _, pending = self._run(self._store(tmp_path))
        assert len(pending) == 1, (
            f"只剩 200 股额度，第 2 条必须拦下（若放行两条，说明已用额度没被消费）——实际落库 {len(pending)} 条")
        assert pending[0]["ticker"] == "AAPL"
        got = pending[0]["result_json"]
        assert got["shares"] == 200, f"剩余 20,000 / 100 元 = 200 股，实际 {got['shares']}"
        assert got.get("auto_adjusted") is True

    def test_本批已放行金额并入已用额度(self, tmp_path):
        """批内累加：第 1 条用满剩余 20,000 后，第 2 条必须看到「额度已尽」而不是各看各的 280,000。

        单看这一条会误以为是「日额度总量校验」；它与上一条的区别正是 N-22 的原始病历——
        「逐笔比总额度」（每条都 20,000 都合规）会让同轮放出 40,000，累计 320,000 > 300,000。
        """
        events, pending = self._run(self._store(tmp_path))
        blocked = [e for e in events if e.get("event") == "decision_done"]
        assert blocked, "L4 必须跑完并给出结论"
        assert len(pending) == 1 and pending[0]["result_json"]["shares"] == 200
        assert "NVDA" not in [p["ticker"] for p in pending]
