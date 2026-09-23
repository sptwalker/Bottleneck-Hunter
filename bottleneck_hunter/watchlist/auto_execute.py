"""L4 自动执行开关 —— 用户开启后，投委会通过的待确认操作免人工确认直接成交。

存储复用 user_preferences 表（key="auto_execute_l4"，已按用户+市场隔离，
见 store_watchlist.save_preference/get_preference）。与持仓风格 persona 同一套机制。

覆盖面：挂在 decision_engine 的 run_daily_decision / run_full_refresh 投委会之后，
故「定时夜间跑批」与「UI 一键决策/全量刷新」两条路径都会在开启时自动成交——
不依赖前端在场（若放前端自动点确认，夜间定时决策就不会自动执行，形同虚设）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

AUTO_EXEC_KEY = "auto_execute_l4"
AUTO_EXEC_CATEGORY = "auto_execute"


def is_auto_execute_enabled(store) -> bool:
    """读取当前用户在该市场是否开启 L4 自动执行（默认关）。"""
    return store.get_preference(AUTO_EXEC_KEY, "") == "1"


def set_auto_execute(store, enabled: bool) -> None:
    store.save_preference(AUTO_EXEC_KEY, "1" if enabled else "0", category=AUTO_EXEC_CATEGORY)


def _sse(event: str, **data) -> dict:
    # 与 decision_engine._sse 同形，避免反向 import 造成循环依赖
    return {"event": event, "data": {"event": event, **data}}


def _is_mandate_exception(ex: dict) -> bool:
    """该执行计划是否标了「越过 L2 目标授权」（P0-4 机会驱动）——这类必须人工确认。"""
    rj = ex.get("result_json") or {}
    if isinstance(rj, str):  # 防御：某些读路径可能给原始字符串
        try:
            rj = json.loads(rj)
        except (ValueError, TypeError):
            return False
    return bool(rj.get("mandate_exception"))


async def auto_execute_pending(store, market: str) -> AsyncGenerator[dict, None]:
    """把当前市场所有待确认执行计划逐条自动成交（confirm+execute 同一条闭环）。

    仅处理 status=pending（投委会已通过、未被拦截/挂单）；逐条独立，单条失败不影响其余。
    未达限价的计划会由 confirm_and_execute 自动转挂单，不算失败。
    """
    from bottleneck_hunter.watchlist.trade_executor import confirm_and_execute

    pending = store.get_pending_executions()
    # P0-4：机会驱动越过 L2 目标授权的计划必须人工确认——自动执行对它一律让路，永远不免确认。
    # 这是护栏的最后一道：护栏①~③ 都在 L3/L4 生成侧，只有这里能拦住「开关一开就全自动成交」。
    exceptions = [ex for ex in pending if _is_mandate_exception(ex)]
    pending = [ex for ex in pending if not _is_mandate_exception(ex)]
    if not pending:
        if exceptions:
            yield _sse("auto_execute_skipped", layer="auto_execute",
                       count=len(exceptions),
                       message=f"{len(exceptions)} 条越线计划需人工确认，已跳过自动执行")
        return

    yield _sse("auto_execute_start", layer="auto_execute",
               count=len(pending),
               exception_count=len(exceptions),
               message=f"已开启自动执行：{len(pending)} 条待确认操作免人工确认直接成交…"
                       + (f"（另 {len(exceptions)} 条越线计划留待人工确认）" if exceptions else ""))

    done = rested = failed = 0
    for ex in pending:
        plan_id = ex.get("id")
        if not plan_id:
            continue
        ticker = ex.get("ticker", "")
        try:
            res = await confirm_and_execute(store, plan_id)
        except Exception as e:  # noqa: BLE001 —— 单条异常不得中断整轮自动执行
            failed += 1
            logger.warning("自动执行异常 plan_id=%s ticker=%s: %s", plan_id, ticker, e)
            continue
        st = res.get("status")
        if st == "confirmed":
            done += 1
        elif st == "resting":
            rested += 1
            yield _sse("auto_execute_item", layer="auto_execute", ticker=ticker,
                       status="resting",
                       message=f"{ticker} 未达限价，已自动转挂单")
        else:
            failed += 1
            msg = res.get("message") or res.get("error") or st or "未知错误"
            yield _sse("auto_execute_item", layer="auto_execute", ticker=ticker,
                       status="failed",
                       message=f"{ticker} 自动执行未成交：{msg}")

    parts = [f"成交 {done} 条"]
    if rested:
        parts.append(f"转挂单 {rested} 条")
    if failed:
        parts.append(f"未成交 {failed} 条")
    yield _sse("auto_execute_done", layer="auto_execute",
               executed=done, rested=rested, failed=failed,
               message="自动执行完成：" + "，".join(parts))


if __name__ == "__main__":
    # ponytail: 自检 —— 开关读写（默认关 / 写 1 读 True / 写 0 读 False）
    class _FakeStore:
        def __init__(self):
            self._kv = {}
        def get_preference(self, key, default=""):
            return self._kv.get(key, default)
        def save_preference(self, key, value, category=""):
            self._kv[key] = value

    s = _FakeStore()
    assert is_auto_execute_enabled(s) is False
    set_auto_execute(s, True)
    assert is_auto_execute_enabled(s) is True
    set_auto_execute(s, False)
    assert is_auto_execute_enabled(s) is False
    print("auto_execute self-check OK")
