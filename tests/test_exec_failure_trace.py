"""P0-B（N-2）执行失败留痕 + pending 老化 回归测试。

病史：成交失败只把状态滚回 pending 就结束了。计划行里没有计数、没有最后错误，
operation_log 一个字都没有 —— 「同一笔单失败 30 次」与「今天刚建还没轮到」
在事后完全无法区分；pending 又只有挂单专用过期逻辑（要求 status=confirmed），
对裸 pending 恒为假，于是无限堆积、无人告警、无人收尸。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from bottleneck_hunter.watchlist import scheduler
from bottleneck_hunter.watchlist.store import WatchlistStore

UID = "u_fail"


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(db_path=tmp_path / "t.db", user_id=UID).for_user(UID).for_market("us_stock")
    yield s
    # oplog 是模块级全局，测完复原，避免污染其它用例
    from bottleneck_hunter.web import oplog

    oplog.set_store(None)


def _mk_plan(store, ticker="AAPL", **kw):
    # strict=False：本组用例只关心状态机与留痕，不牵扯研究快照绑定
    kw.setdefault("strict", False)
    return store.create_execution_plan(
        "tp1", "e1", ticker, {"action": "buy", "shares": 10, "target_price": 100.0}, **kw
    )


def _backdate(store, plan_id, days):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with store._write_conn() as conn:
        conn.execute("UPDATE execution_plans SET created_at = ? WHERE id = ?", (old, plan_id))


class TestFailureTrace:
    def test计数与最后错误落库(self, store):
        pid = _mk_plan(store)
        assert store.record_execution_failure(pid, "现金不足") is True
        assert store.record_execution_failure(pid, "现金不足") is True
        row = store.get_execution_plan(pid)
        assert row["attempt_count"] == 2          # 累加，不是覆盖
        assert row["last_error"] == "现金不足"

    def test_不存在的计划返回假(self, store):
        assert store.record_execution_failure("没这个计划", "x") is False

    def test_超长错误被截断(self, store):
        """last_error 落库前截断，避免一条异常栈撑爆行。"""
        pid = _mk_plan(store)
        store.record_execution_failure(pid, "E" * 5000)
        assert len(store.get_execution_plan(pid)["last_error"]) == 500

    def test_成交失败落三处痕迹(self, store):
        """_record_exec_failure：计划行计数 + operation_log(error，进推送白名单)。

        走真实 execute_trade 的失败路径（无真实市价快照 → 拒绝以 LLM 估价成交）。
        """
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        pid = _mk_plan(store)
        fake = MagicMock()
        fake.get_execution_plan.return_value = {"snapshot_id": "s1", "strategy_version": "v1",
                                                "action": "buy", "ticker": "AAPL",
                                                "shares": 10, "target_price": 100.0,
                                                "result_json": {}}
        fake.get_sim_account.return_value = {"id": "acc1", "cash_balance": 100000,
                                             "initial_capital": 100000, "total_equity": 100000}
        fake.get_latest_snapshot.return_value = None   # 无真实快照
        fake.get_research_snapshot.return_value = MagicMock(strategy_version="v1")
        fake.for_market.return_value = fake
        fake._user_id = UID
        fake._market = "us_stock"
        # 计数与留痕都写真实库，只有读路径走桩，故这里把真实 store 的写方法接上去
        fake.record_execution_failure = store.record_execution_failure

        from bottleneck_hunter.watchlist.constraint_validator import ValidationResult
        from bottleneck_hunter.watchlist.trade_executor import execute_trade

        with patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan",
                   lambda *a, **k: ValidationResult()):
            res = execute_trade(fake, pid)

        assert "无真实市价快照" in res["error"]          # 前提：确实走在失败路径上
        assert store.get_execution_plan(pid)["attempt_count"] == 1
        ops = store.get_operations(UID, category="error")
        assert [o["title"] for o in ops] == ["执行计划成交失败"]
        assert "无真实市价快照" in ops[0]["detail"]

    def test_计数写入失败不影响成交主流程(self):
        """留痕是旁路：store 不支持计数时不得把成交拖挂。"""
        from bottleneck_hunter.watchlist.trade_executor import _record_exec_failure

        broken = MagicMock()
        broken._user_id = UID
        broken.record_execution_failure.side_effect = RuntimeError("db locked")
        _record_exec_failure(broken, "p1", "随便")   # 不抛

    def test_无用户上下文则跳过留痕(self):
        """未绑定用户的 store（_user_id 为空）不能往别人日志里写。"""
        from bottleneck_hunter.watchlist.trade_executor import _record_exec_failure

        s = MagicMock()
        s._user_id = ""
        _record_exec_failure(s, "p1", "随便")
        s.record_execution_failure.assert_called_once()   # 计数仍写，日志不写


def _resting_back_to_pending(store, ticker="REST"):
    """造出「pending 且 resting_until 非空」的计划 —— 走真实状态机，不手工刷 status。"""
    pid = _mk_plan(store, ticker)
    store.confirm_execution(pid)
    store.rest_execution(pid, "2099-01-01T00:00:00+00:00")
    store.reject_execution(pid, "[投委会] 测试")
    store.restore_execution(pid)
    row = store.get_execution_plan(pid)
    assert row["status"] == "pending" and row["resting_until"], \
        "前提不成立：本用例要的正是「pending 且带挂单标记」这个状态"
    return pid


class TestStalePendingReaper:
    def test挂单退回pending后成孤儿_两条收尾路径都看不见它(self, store):
        """`pending` 且 resting_until 非空 **在生产可达**，不是防御性分支 —— 两条真实路径：

        ① 投委会质询改判否决/用户 override → restore_execution 置 pending，不清挂单标记；
        ② 挂单成交失败 → revert_to_pending 置 pending，同样不清。

        两处都**刻意**不清：rest_execution 的『重复调用不重置(避免续期)』是防续期设计，
        清掉标记会让每轮失败都续出新的 14 天窗口。
        代价是这类计划 reaper 不收、挂单轮询也看不见（get_resting_executions 要求 confirmed）——
        故 `COALESCE(resting_until,'')=''` 是**承重**子句，摘掉它等于把挂单扔进收尸队列。
        """
        pid = _resting_back_to_pending(store)
        _backdate(store, pid, 20)
        assert "2099" in store.get_execution_plan(pid)["resting_until"]
        assert store.get_resting_executions() == []      # 轮询看不见它（要求 confirmed）
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        assert store.get_stale_pending_executions(cutoff) == []   # 收尸也不能收它

    def test只收早于cutoff且非挂单的(self, store):
        """「非挂单」这一支必须真被执行到。

        病史（变异 MUTB2 实证）：本用例原先把「挂单」那条造成 status='confirmed'，
        于是它先被 `status='pending'` 排除了 —— 断言绿着，而
        `COALESCE(resting_until,'')=''` 这一支**一次都没跑过**：把它从 SQL 里整条摘掉，
        全量 2284 条测试无一报警。故改为构造真实可达的「pending + 挂单标记」。
        """
        old_p = _mk_plan(store, "OLD")
        fresh_p = _mk_plan(store, "NEW")
        _backdate(store, old_p, 20)
        old_rest = _resting_back_to_pending(store, "REST")
        _backdate(store, old_rest, 20)

        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        ids = {p["id"] for p in store.get_stale_pending_executions(cutoff)}
        assert old_p in ids              # 裸滞留 pending：收
        assert fresh_p not in ids        # 刚建的不算滞留
        assert old_rest not in ids       # pending 但仍是挂单：轮询/收尸都不碰，不得误收

    def testconfirmed状态不收(self, store):
        pid = _mk_plan(store)
        _backdate(store, pid, 20)
        with store._write_conn() as conn:
            conn.execute("UPDATE execution_plans SET status = 'confirmed' WHERE id = ?", (pid,))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        assert store.get_stale_pending_executions(cutoff) == []

    def test挂单专用过期对裸pending无效(self, store):
        """这正是堆积根因：expire_execution 要求 status=confirmed，裸 pending 永远走不到它。"""
        pid = _mk_plan(store)
        assert store.expire_execution(pid, "过期") is False
        assert store.expire_stale_pending(pid, "过期") is True
        row = store.get_execution_plan(pid)
        assert row["status"] == "expired"
        assert row["rejection_reason"] == "过期"

    def test任务收尸并留痕(self, store):
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        pid = _mk_plan(store)
        _backdate(store, pid, 20)
        store.record_execution_failure(pid, "限价未达")
        store.expire_stale_pending("不存在的", "x")     # 幂等：无此计划不炸

        with patch.object(scheduler, "_iter_users", return_value=iter([(UID, store, MagicMock())])):
            asyncio.run(scheduler.job_expire_stale_pending())

        assert store.get_execution_plan(pid)["status"] == "expired"
        ops = store.get_operations(UID, category="error")
        titles = [o["title"] for o in ops]
        assert "待确认计划滞留过期" in titles
        detail = next(o["detail"] for o in ops if o["title"] == "待确认计划滞留过期")
        assert "累计尝试 1 次" in detail and "限价未达" in detail

    def test任务对干净库不出声(self, store):
        """没有滞留计划时不写日志、不推 IM —— 否则每天一条噪声把真告警淹掉。"""
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        with patch.object(scheduler, "_iter_users", return_value=iter([(UID, store, MagicMock())])):
            asyncio.run(scheduler.job_expire_stale_pending())
        assert store.get_operations(UID, category="error") == []

    def test单用户异常不中断其余用户(self, store):
        from bottleneck_hunter.web import oplog

        oplog.set_store(store)
        pid = _mk_plan(store)
        _backdate(store, pid, 20)
        boom = MagicMock()
        boom.get_stale_pending_executions.side_effect = RuntimeError("boom")
        with patch.object(scheduler, "_iter_users",
                          return_value=iter([("u_boom", boom, MagicMock()), (UID, store, MagicMock())])):
            asyncio.run(scheduler.job_expire_stale_pending())
        assert store.get_execution_plan(pid)["status"] == "expired"


def test注册表四处齐全():
    """新任务必须同时出现在 _JOB_SPECS / list_job_categories / list_job_labels / 全局时间表。

    第四处（`GLOBAL_SCHEDULE_DEFAULTS`）是实际漏过一次的那处：漏登记不会报错，但前端时间表
    是**按这份表遍历**渲染的（`auto-update.js` 的 `Object.entries(global_schedule)`），
    于是该任务在界面上**整条不出现**、管理员也无从调它 —— 静默的半上线。
    """
    from bottleneck_hunter.watchlist.schedule_config import GLOBAL_SCHEDULE_DEFAULTS

    ids = [s[0] for s in scheduler._JOB_SPECS]
    assert "expire_stale_pending" in ids
    assert scheduler.list_job_categories().get("expire_stale_pending") == "daily_decision"
    assert "expire_stale_pending" in scheduler.list_job_labels()
    assert "expire_stale_pending" in GLOBAL_SCHEDULE_DEFAULTS


def test时间表登记与任务表一一对应():
    """反向也要钉住：两表漂移时（新增任务忘登记 / 表里留着已删任务）必须变红。

    这正是上面那条"整条不出现"的根因——两处清单各写各的、无人比对。
    """
    from bottleneck_hunter.watchlist.schedule_config import GLOBAL_SCHEDULE_DEFAULTS

    ids = {s[0] for s in scheduler._JOB_SPECS}
    assert ids == set(GLOBAL_SCHEDULE_DEFAULTS), (
        f"只在任务表: {sorted(ids - set(GLOBAL_SCHEDULE_DEFAULTS))}；"
        f"只在时间表: {sorted(set(GLOBAL_SCHEDULE_DEFAULTS) - ids)}"
    )
