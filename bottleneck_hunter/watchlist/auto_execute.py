"""L4 自动执行开关（三档）—— 关闭 / 半授权 / 高授权。

- 关闭（0）：投委会通过的待确认操作全部留给你人工确认（默认）。
- 半授权（1）：免人工确认直接成交，但**越过 L2 目标授权**的机会驱动计划仍然留人工确认。
- 高授权（2）：连越线计划也一并自动成交。

存储复用 user_preferences 表（key="auto_execute_l4"，已按用户+市场隔离，
见 store_watchlist.save_preference/get_preference）。与持仓风格 persona 同一套机制。
取值为数字档位字符串；历史遗留的 "1"/"" 恰好分别等价于半授权/关闭，无需迁移。

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

LEVEL_OFF = 0        # 关闭：全部人工确认
LEVEL_SEMI = 1       # 半授权：免确认，但越线计划仍人工确认
LEVEL_HIGH = 2       # 高授权：越线计划也自动成交
MAX_LEVEL = LEVEL_HIGH


def get_auto_execute_level(store) -> int:
    """读取当前用户在该市场的自动执行档位（0~2，默认 0）。

    读路径一律降级（fail-closed）：脏值、越界值（"3"/"-1"）全部当关闭处理——
    读到的值不可信时宁可多要一次人工确认，绝不因为一个坏值静默提权到自动成交。
    """
    raw = str(store.get_preference(AUTO_EXEC_KEY, "") or "").strip()
    try:
        lv = int(raw)
    except ValueError:
        return LEVEL_OFF
    return lv if LEVEL_OFF <= lv <= MAX_LEVEL else LEVEL_OFF


def set_auto_execute_level(store, level: int) -> None:
    """写入档位（写路径钳位到合法区间，越界即夹到最近端点）。"""
    lv = max(LEVEL_OFF, min(MAX_LEVEL, int(level)))
    store.save_preference(AUTO_EXEC_KEY, str(lv), category=AUTO_EXEC_CATEGORY)


def is_auto_execute_enabled(store) -> bool:
    """档位 ≥ 半授权即为「开着」（决定要不要跑自动执行这一轮）。"""
    return get_auto_execute_level(store) >= LEVEL_SEMI


def set_auto_execute(store, enabled: bool) -> None:
    """布尔兼容入口（老调用方/老前端）：True→半授权，False→关闭。"""
    set_auto_execute_level(store, LEVEL_SEMI if enabled else LEVEL_OFF)


def is_breach_authorized(store) -> bool:
    """是否授权自动执行「越过 L2 目标」的越线操作（仅高授权档为真）。"""
    return get_auto_execute_level(store) >= LEVEL_HIGH


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
    # P0-4：机会驱动越过 L2 目标授权的计划默认必须人工确认——这是护栏的最后一道
    # （护栏①~③ 都在 L3/L4 生成侧，只有这里能拦住「开关一开就全自动成交」）。
    # 高授权档才把这道闸打开：越线计划也一并自动成交。
    if is_breach_authorized(store):
        exceptions: list[dict] = []
    else:
        exceptions = [ex for ex in pending if _is_mandate_exception(ex)]
        pending = [ex for ex in pending if not _is_mandate_exception(ex)]
    if not pending:
        if exceptions:
            yield _sse("auto_execute_skipped", layer="auto_execute",
                       count=len(exceptions),
                       message=f"{len(exceptions)} 条越线计划需人工确认，已跳过自动执行")
        return

    _high = is_breach_authorized(store)
    yield _sse("auto_execute_start", layer="auto_execute",
               count=len(pending),
               exception_count=len(exceptions),
               level=LEVEL_HIGH if _high else LEVEL_SEMI,
               message=f"已开启自动执行（{'高授权：含越线' if _high else '半授权'}）："
                       f"{len(pending)} 条待确认操作免人工确认直接成交…"
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
    # ponytail: 自检 —— 三档读写 + 布尔兼容 + 脏值降级
    class _FakeStore:
        def __init__(self):
            self._kv = {}
        def get_preference(self, key, default=""):
            return self._kv.get(key, default)
        def save_preference(self, key, value, category=""):
            self._kv[key] = value

    s = _FakeStore()
    assert is_auto_execute_enabled(s) is False and is_breach_authorized(s) is False  # 默认关
    set_auto_execute(s, True)                                                        # 布尔兼容 → 半授权
    assert get_auto_execute_level(s) == LEVEL_SEMI
    assert is_auto_execute_enabled(s) is True and is_breach_authorized(s) is False
    set_auto_execute_level(s, LEVEL_HIGH)
    assert is_breach_authorized(s) is True
    set_auto_execute_level(s, 99)                                                    # 越界钳位
    assert get_auto_execute_level(s) == LEVEL_HIGH
    set_auto_execute_level(s, LEVEL_OFF)
    assert is_auto_execute_enabled(s) is False
    s.save_preference(AUTO_EXEC_KEY, "垃圾")                                          # 脏值 → 最保守
    assert get_auto_execute_level(s) == LEVEL_OFF
    print("auto_execute self-check OK")
