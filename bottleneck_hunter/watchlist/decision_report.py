"""每日决策回执 —— 让「正常运行但没操作」和「出故障没产出」在日志里长得不一样。

**Why**：原先调度器只看 `_drain_sse` 的 `had_fatal`（仅「无可用 LLM」为真），于是
「质量门红灯阻断 L4」「今日无 L3 计划」「L3 全为持有」「预算不足」全部被记成
`success` + 「N 只标的完成 L1→L4+投委会」。用户看到 success，实际连续多日没有任何
执行方案产出——生产上曾因此让美股 L4 静默停摆多日无人察觉。

**How**：`_drain_sse` 在消费 SSE 时顺手把终态事实（L4 是否产出、是否被质量门 red 阻断、
L4 打出的跳过原因）收集进一个 dict；`build_report` 据此产出一句人话，交给
`record_operation` 落进「实时操作日志」。终态文案刻意区分「✅ 运行正常」与「⛔ 被阻断」，
避免再次把故障涂成常态。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# L4 提前返回/正常收尾时 `decision_done`/`decision_info` 的 message 特征 → 终态语义。
# 顺序即优先级：先判「被阻断」，再判「没到 L4」，最后才是「跑了但选择不动手」。
_NO_TACTICAL_HINT = "无 L3 战术计划"
_ALL_HOLD_HINT = "全部为持有"


def empty_summary() -> dict:
    """`_drain_sse` 用的累加器初值。"""
    return {
        "l4_plan_count": 0,        # L4 落库的待确认执行方案条数
        "l4_blocked_count": 0,     # 被风控拦截的执行方案条数
        "l4_repaired_count": 0,    # 自动修好的条数
        "l4_message": "",          # L4 收尾/提前返回的原话
        "l4_ran": False,           # L4 是否真正进入并产出（含"跑了但没操作"）
        "l3_plan_count": -1,       # L3 战术计划条数（-1 = 本轮没到 L3）
        "quality_blocked": "",     # pre_l4 质量门红灯原因（非空 = L4 被阻断）
        "llm_error": "",           # （预留）无可用 LLM 的原话
        "budget_error": False,     # 预算不足提前返回
        "errors": [],              # 其它 decision_error 摘要
    }


def summarize_event(data: dict, summary: dict) -> None:
    """把一条 SSE 事件并入 summary。就地修改，不返回值。"""
    event = data.get("event", "")
    if event == "quality_check_block" and data.get("stage") == "pre_l4":
        summary["quality_blocked"] = data.get("reason") or data.get("message") or "质量门红灯"
        return
    if event in ("decision_done", "decision_info", "decision_error"):
        layer = data.get("layer", "")
        if layer == "L3" and event == "decision_done":
            summary["l3_plan_count"] = int(data.get("plan_count", 0) or 0)
            return
        if layer != "L4":
            if event == "decision_error" and data.get("error"):
                summary["errors"].append(str(data["error"])[:120])
            return
        if event == "decision_done":
            summary["l4_ran"] = True
            summary["l4_plan_count"] = int(data.get("plan_count", 0) or 0)
            summary["l4_blocked_count"] = int(data.get("blocked_count", 0) or 0)
            summary["l4_repaired_count"] = int(data.get("repaired_count", 0) or 0)
            summary["l4_message"] = str(data.get("message", ""))
        elif event == "decision_info":
            summary["l4_message"] = str(data.get("message", ""))
        elif event == "decision_error":
            err = str(data.get("error", ""))
            summary["l4_message"] = err
            if "无可用 LLM" in err:
                summary["llm_error"] = err
            elif "预算" in err:
                summary["budget_error"] = True
            else:
                summary["errors"].append(err[:120])


def build_report(summary: dict, *, market: str, ticker_count: int = 0) -> tuple[str, str, str]:
    """把 summary 折成 (result, title, detail)。

    result：success = 系统正常且无待办；partial = 跑完了但没产出/被拦（需用户看一眼）；
            fail = 链路缺失（无 L3 计划 / 无 LLM / 预算不足）。
    """
    mkt = "美股" if market == "us_stock" else ("A股" if market == "a_stock" else market)
    scope_txt = f"{ticker_count} 只标的" if ticker_count else "本次"

    if summary.get("quality_blocked"):
        return (
            "partial",
            "日常决策（被质量门阻断）",
            f"{mkt} {scope_txt}跑完 L1→L3，但 pre_l4 质量门红灯，L4 新建执行计划被整体阻断，"
            f"本次无操作。原因：{summary['quality_blocked']}",
        )

    if summary.get("llm_error"):
        return ("fail", "日常决策（无可用 LLM）", f"{mkt} 决策链无可用 LLM：{summary['llm_error']}")
    if summary.get("budget_error"):
        return ("fail", "日常决策（预算不足）", f"{mkt} LLM 预算不足，L4 未生成执行方案")

    if summary.get("l4_ran"):
        plans = summary.get("l4_plan_count", 0)
        if plans > 0:
            extra = f"，另有 {summary['l4_blocked_count']} 条被风控拦截" if summary.get("l4_blocked_count") else ""
            return ("success", "日常决策", f"{mkt} {scope_txt}完成 L1→L4+投委会，"
                                          f"✅ 已生成 {plans} 条待确认操作{extra}")
        # 关键分支：运行正常、LLM 主动选择不动手 —— 必须与「被阻断」区分开。
        return ("success", "日常决策（运行正常·本次无操作）",
                f"{mkt} {scope_txt}完成 L1→L4+投委会，✅ 系统运行正常，LLM 判断本次无需操作"
                f"（{summary.get('l4_message') or '无符合条件的执行方案'}）")

    msg = summary.get("l4_message") or "未产出执行方案"
    if _NO_TACTICAL_HINT in msg:
        return ("fail", "日常决策（无 L3 战术计划）",
                f"{mkt} {scope_txt}跑到 L4，但今日无 L3 战术计划，L4 跳过：{msg}")
    if _ALL_HOLD_HINT in msg:
        return ("success", "日常决策（运行正常·L3 全为持有）",
                f"{mkt} {scope_txt}完成 L1→L3，L3 计划全部为持有，无需生成执行方案（系统运行正常）")

    if not summary.get("l4_message"):
        return ("fail", "日常决策（未跑到 L4）",
                f"{mkt} {scope_txt}流程未到达 L4（可能在数据时效门或更早阶段中断），本次未产出执行方案")
    return ("fail", "日常决策（L4 未产出）", f"{mkt} {scope_txt}：{msg}")


def report_daily_decision(uid: str, market: str, summary: dict, ticker_count: int = 0) -> None:
    """落一条每日决策回执到「实时操作日志」。失败不影响调度。"""
    if not uid or not summary:
        return
    try:
        from bottleneck_hunter.web.oplog import record_operation

        result, title, detail = build_report(summary, market=market, ticker_count=ticker_count)
        if result == "fail":
            record_operation(uid, title, category="error", detail=detail[:300],
                             result="fail", market=market)
        else:
            record_operation(uid, title, category="auto_update", detail=detail[:300],
                             result=result, market=market)
    except Exception as e:  # noqa: BLE001
        logger.debug("每日决策回执记录失败: %s", e)
