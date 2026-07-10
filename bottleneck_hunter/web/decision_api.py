"""决策中心 API — 挂载于 /api/decision

提供 L1-L4 决策引擎、催化剂管理、风险仪表盘等端点。
模拟交易相关端点已迁移至 trading_api.py (/api/trading)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.llm_clients.factory import PROVIDER_MODELS

from bottleneck_hunter.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["decision"])

_store: WatchlistStore | None = None


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def _get_store() -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="WatchlistStore not initialized")
    return _store


def _user_store(user: dict) -> WatchlistStore:
    """返回绑定当前用户的 store 实例。"""
    return _get_store().for_user(user["sub"])


def _maybe_json(v):
    """若 v 是 JSON 字符串则解析，解析失败返回 {}；非字符串按原样返回。"""
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return {}


def _user_budget(user: dict):
    """返回绑定当前用户的 BudgetTracker。"""
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    return BudgetTracker(_user_store(user))


def _budget():
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    return BudgetTracker(_get_store())


def _sse_response(request: Request, gen_coro):
    from bottleneck_hunter.llm_clients.fallback import begin_notices, drain_notices

    async def event_generator():
        begin_notices()

        def _flush():
            for n in drain_notices():
                yield {"event": "model_fallback", "data": json.dumps(n, ensure_ascii=False)}

        async for evt in gen_coro:
            if await request.is_disconnected():
                break
            yield {
                "event": evt.get("event", "progress"),
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }
            for e in _flush():
                yield e
        for e in _flush():
            yield e
    return EventSourceResponse(event_generator())


# ─────────────────────────────────────────────────────────
# L1 宏观策略
# ─────────────────────────────────────────────────────────

@router.post("/macro/generate")
async def generate_macro_strategy(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """全面生成 L1 宏观策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_macro_strategy(store, _user_budget(user), market=market))


@router.post("/macro/check")
async def check_macro_strategy(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """L1 日常检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_check
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_macro_check(store, _user_budget(user), market=market))


@router.get("/macro/latest")
async def get_latest_macro(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """获取最新 L1 宏观策略"""
    store = _user_store(user).for_market(market)
    strategy = store.get_latest_macro_strategy()
    if not strategy:
        return {"strategy": None, "message": "尚未生成宏观策略"}
    return {"strategy": strategy}


@router.get("/macro/history")
async def get_macro_history(limit: int = 10, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"history": store.get_macro_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# L1 宏观咨询互动（两位分析师流式多轮对话，每市场一条滚动会话）
# ─────────────────────────────────────────────────────────

class MacroConsultAsk(BaseModel):
    market: str = "us_stock"
    question: str = ""


@router.post("/macro/consult/open")
async def macro_consult_open(request: Request, market: str = "us_stock",
                             user: dict = Depends(get_current_user)):
    """打开咨询抽屉：陈列 L1 数据快照 + 两位分析师自动流式开场解读（SSE）。"""
    from bottleneck_hunter.watchlist.macro_consultation import stream_opening
    store = _user_store(user).for_market(market)
    return _sse_response(request, stream_opening(store, _user_budget(user), market))


@router.post("/macro/consult/ask")
async def macro_consult_ask(request: Request, req: MacroConsultAsk,
                            user: dict = Depends(get_current_user)):
    """用户提问：round1 各自作答 → round2 互评辩论（SSE）。"""
    from bottleneck_hunter.watchlist.macro_consultation import stream_consult
    store = _user_store(user).for_market(req.market)
    return _sse_response(request, stream_consult(store, _user_budget(user), req.market, req.question))


@router.get("/macro/consult/history")
async def macro_consult_history(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """取该市场的滚动会话（含完整 transcript）。stale=新闻库已有更新新闻，前端据此决定是否重开生成。"""
    from bottleneck_hunter.watchlist.macro_consultation import _load_session, snapshot_is_stale
    store = _user_store(user).for_market(market)
    session = _load_session(store, market)
    return {"session": session, "stale": snapshot_is_stale(store, market, session)}


# ─────────────────────────────────────────────────────────
# L2 组合策略
# ─────────────────────────────────────────────────────────

@router.post("/strategic/generate")
async def generate_strategic_plan(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """全面生成 L2 组合策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_strategic_plan(store, _user_budget(user), market=market))


@router.post("/strategic/deviation-check")
async def deviation_check(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """L2 偏离检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_deviation_check
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_deviation_check(store, _user_budget(user), market=market))


@router.get("/strategic/latest")
async def get_latest_strategic(market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    plan = store.get_latest_strategic_plan()
    if not plan:
        return {"plan": None, "message": "尚未生成组合策略"}
    return {"plan": plan}


@router.get("/strategic/history")
async def get_strategic_history(limit: int = 10, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"history": store.get_strategic_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# 日常决策流程
# ─────────────────────────────────────────────────────────

class DailyRequest(BaseModel):
    scope: str = "full"
    market: str = "us_stock"


@router.post("/daily")
async def run_daily(request: Request, body: DailyRequest | None = None, user: dict = Depends(get_current_user)):
    """执行日常决策流程 L1→L2→L3→L4→投委会（SSE 流）

    scope: "full" | "l1" | "l3l4"
    """
    from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
    market = body.market if body else "us_stock"
    scope = body.scope if body else "full"
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_daily_decision(store, _user_budget(user), scope=scope, market=market))


@router.post("/full-refresh")
async def full_refresh(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """全量刷新 L1+L2+L3+L4+投委会（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_full_refresh
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_full_refresh(store, _user_budget(user), market=market))


# ─────────────────────────────────────────────────────────
# L3 战术计划
# ─────────────────────────────────────────────────────────

@router.post("/tactical/generate")
async def generate_tactical(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """生成 L3 战术计划（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_tactical_plans(store, _user_budget(user), market=market))


@router.get("/tactical/latest")
async def get_latest_tactical(market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plans = store.get_tactical_plans_by_date(today)
    if not plans:
        return {"plans": [], "message": "今日尚未生成战术计划"}
    return {"plans": plans}


@router.get("/tactical/{ticker}")
async def get_tactical_for_ticker(ticker: str, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    plan = store.get_tactical_plan_for_ticker(ticker)
    if not plan:
        return {"plan": None, "message": f"{ticker} 无战术计划"}
    return {"plan": plan}


# ─────────────────────────────────────────────────────────
# L4 执行方案
# ─────────────────────────────────────────────────────────

@router.post("/execution/generate")
async def generate_execution(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """生成 L4 执行方案（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_execution_plans(store, _user_budget(user), market=market))


@router.get("/execution/{plan_id}")
async def get_execution_detail(plan_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    plan = store.get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="执行计划不存在")
    reviews = store.get_reviews_for_execution(plan_id)
    return {"plan": plan, "reviews": reviews}


# ─────────────────────────────────────────────────────────
# 投委会
# ─────────────────────────────────────────────────────────

@router.post("/committee/review")
async def trigger_committee_review(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """对所有待审执行计划启动投委会评审（SSE 流）"""
    store = _user_store(user).for_market(market)
    pending = store.get_pending_executions()
    if not pending:
        return {"message": "无待审执行计划"}
    from bottleneck_hunter.watchlist.committee import run_committee_review
    return _sse_response(request, run_committee_review(store, pending, _user_budget(user), market=market))


@router.get("/committee/reviews/{plan_id}")
async def get_committee_reviews(plan_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    reviews = store.get_reviews_for_execution(plan_id)
    return {"reviews": reviews}


@router.get("/committee/consensus/{plan_id}")
async def get_committee_consensus(plan_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    conn = store._connect()
    try:
        q = "SELECT * FROM committee_consensus WHERE execution_plan_id = ? ORDER BY created_at DESC LIMIT 1"
        p = (plan_id,)
        q, p = store._user_filter(q, p)
        row = conn.execute(q, p).fetchone()
        if not row:
            return {"consensus": None, "message": "暂无共识结果"}
        d = dict(row)
        for f in ("vote_detail", "consensus_modifications", "final_execution_plan",
                  "key_risks_flagged", "minority_opinions", "result_json"):
            if f in d and isinstance(d[f], str):
                try:
                    d[f] = json.loads(d[f])
                except (json.JSONDecodeError, TypeError):
                    pass
        return {"consensus": d}
    finally:
        conn.close()


class CommitteeChallengeRequest(BaseModel):
    meeting_id: str
    role: str
    message: str
    market: str = "us_stock"


@router.post("/committee/challenge")
async def committee_challenge(req: CommitteeChallengeRequest, user: dict = Depends(get_current_user)):
    """用户对某投委会成员发起质询；成员可被说服改票 → 重算加权共识 → 重新 gating。"""
    from bottleneck_hunter.watchlist.committee import challenge_member
    store = _user_store(user).for_market(req.market)
    result = await challenge_member(
        store, meeting_id=req.meeting_id, role=req.role,
        user_message=req.message, market=req.market)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ─────────────────────────────────────────────────────────
# 催化剂
# ─────────────────────────────────────────────────────────

@router.post("/catalysts/scan")
async def scan_catalysts(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """扫描观察池提取催化剂（SSE 流）"""
    from bottleneck_hunter.watchlist.catalyst_monitor import detect_catalysts
    store = _user_store(user).for_market(market)
    return _sse_response(request, detect_catalysts(store, _user_budget(user)))


@router.get("/catalysts/upcoming")
async def get_upcoming_catalysts(days: int = 14, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"catalysts": store.get_upcoming_catalysts(days=days)}


@router.get("/catalysts/weekly-preview")
async def get_weekly_preview(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """周度催化剂前瞻 — 按来源分组、按影响度排序"""
    from bottleneck_hunter.watchlist.catalyst_monitor import generate_weekly_preview
    store = _user_store(user).for_market(market)
    return generate_weekly_preview(store)


@router.get("/catalysts/enhanced-calendar")
async def get_enhanced_calendar(days: int = 30, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """增强版催化剂日历 — 含四维分类"""
    from bottleneck_hunter.watchlist.catalyst_monitor import get_catalyst_calendar
    store = _user_store(user).for_market(market)
    return get_catalyst_calendar(store, days=days)


@router.get("/catalysts/calendar")
async def get_catalysts_calendar(month: str | None = None, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """按日期分组返回催化剂事件，格式：{"2026-06-24": [...], ...}"""
    store = _user_store(user).for_market(market)
    from datetime import datetime as _dt, timezone as _tz
    import calendar

    if month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
        except (ValueError, IndexError):
            year = _dt.now(_tz.utc).year
            mon = _dt.now(_tz.utc).month
    else:
        now = _dt.now(_tz.utc)
        year, mon = now.year, now.month

    _, last_day = calendar.monthrange(year, mon)
    start_date = f"{year}-{mon:02d}-01"
    end_date = f"{year}-{mon:02d}-{last_day:02d}"

    conn = store._connect()
    try:
        q, p = store._filtered(
            """SELECT ct.*, w.company_name FROM catalyst_tracking ct
               LEFT JOIN watchlist w ON ct.entry_id = w.id
               WHERE ct.expected_date IS NOT NULL
               AND ct.expected_date >= ? AND ct.expected_date <= ?
               ORDER BY ct.expected_date ASC""",
            (start_date, end_date),
            table="ct",
        )
        rows = conn.execute(q, p).fetchall()
    finally:
        conn.close()

    calendar_data: dict[str, list] = {}
    for r in rows:
        d = dict(r)
        date_key = (d.get("expected_date") or "")[:10]
        if date_key:
            if date_key not in calendar_data:
                calendar_data[date_key] = []
            calendar_data[date_key].append({
                "id": d.get("id"),
                "ticker": d.get("ticker"),
                "company_name": d.get("company_name", ""),
                "title": d.get("title"),
                "catalyst_type": d.get("catalyst_type"),
                "impact_level": d.get("impact_level"),
                "status": d.get("status"),
            })

    return {
        "year": year,
        "month": mon,
        "start_date": start_date,
        "end_date": end_date,
        "events": calendar_data,
    }


@router.get("/catalysts/{entry_id}")
async def get_entry_catalysts(entry_id: str, active_only: bool = True, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"catalysts": store.get_catalysts_for_entry(entry_id, active_only=active_only)}


class UpdateCatalystRequest(BaseModel):
    status: str
    outcome: str = ""
    actual_date: str | None = None


@router.patch("/catalysts/item/{catalyst_id}")
async def update_catalyst(catalyst_id: str, req: UpdateCatalystRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.update_catalyst_status(catalyst_id, req.status, req.outcome, req.actual_date)
    if not ok:
        raise HTTPException(status_code=404, detail="催化剂不存在")
    return {"status": "updated"}


# ─────────────────────────────────────────────────────────
# 执行计划 (L4)
# ─────────────────────────────────────────────────────────

@router.get("/executions/pending")
async def get_pending_executions(market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"executions": store.get_pending_executions()}


@router.get("/executions/blocked")
async def get_blocked_executions(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """被系统/投委会拦截的执行计划（已拦截区）。"""
    store = _user_store(user).for_market(market)
    return {"executions": store.get_blocked_executions()}


@router.post("/executions/{plan_id}/restore")
async def restore_execution(plan_id: str, user: dict = Depends(get_current_user)):
    """用户手动恢复被拦截的计划到待确认队列（override）。"""
    store = _user_store(user)
    ok = store.restore_execution(plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="计划不存在或非拦截状态")
    return {"status": "restored"}


@router.post("/executions/{plan_id}/confirm")
async def confirm_execution(plan_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.confirm_execution(plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="执行计划不存在或已处理")
    from bottleneck_hunter.watchlist.trade_executor import execute_trade
    try:
        trade_result = execute_trade(store, plan_id)
    except Exception as e:
        logger.exception("交易执行异常 plan_id=%s", plan_id)
        # P2.2 执行失败回滚到 pending，避免卡在 confirmed
        try:
            store.revert_to_pending(plan_id)
        except Exception:
            logger.warning("回滚 plan_id=%s 到 pending 失败", plan_id)
        raise HTTPException(status_code=500, detail=f"交易执行异常: {e}") from e
    # execute_trade 返回 error 字段表示业务错误（约束不通过、现金不足等）
    if "error" in trade_result:
        return {"status": "error", "trade": trade_result, "message": trade_result["error"]}
    # 成交成功 → 拉实时价重算持仓（实现"批准操作后实时更新持仓"）。失败静默降级不阻断返回。
    try:
        from bottleneck_hunter.watchlist.trade_executor import refresh_positions_live
        await refresh_positions_live(store)
    except Exception:
        logger.warning("确认后实时刷新持仓失败 plan_id=%s", plan_id)
    return {"status": "confirmed", "trade": trade_result}


class RejectRequest(BaseModel):
    reason: str = ""


@router.post("/executions/{plan_id}/reject")
async def reject_execution(plan_id: str, req: RejectRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.reject_execution(plan_id, req.reason)
    if not ok:
        raise HTTPException(status_code=404, detail="执行计划不存在或已处理")
    return {"status": "rejected"}


@router.post("/executions/clear-all")
async def clear_all_pending(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    count = store.clear_pending_executions()
    return {"status": "ok", "cleared": count}


# ─────────────────────────────────────────────────────────
# 策略概览（整合 L1+L2+L3+L4+投委会+催化剂）
# ─────────────────────────────────────────────────────────

@router.get("/overview")
async def decision_overview(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """策略中心概览 — 一次请求拿到所有关键数据"""
    store = _user_store(user).for_market(market)

    macro = store.get_latest_macro_strategy()
    strategic = store.get_latest_strategic_plan()
    pending = store.get_pending_executions()
    catalysts = store.get_upcoming_catalysts()
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tactical_plans = store.get_tactical_plans_by_date(today)

    # 最近一次投委会评审（从会议 transcript 提取各委员投票，供投委会面板展示）
    committee = []
    committee_meta = None
    try:
        recs = store.get_meeting_records(meeting_type="committee", limit=1)
        if recs:
            rec = recs[0]
            transcript = rec.get("transcript_json", []) or []
            # 取每位委员的最新一轮（round2 改票、round3 质询改票 优先于 round1 首轮），展示终票
            latest_by_role: dict = {}
            for t in transcript:
                role = t.get("role", "")
                if not role or role.startswith("_"):
                    continue
                if t.get("type") == "challenge":
                    continue  # 质询元数据条目无 vote 字段，跳过
                if t.get("round") not in (1, 2, 3):
                    continue
                prev = latest_by_role.get(role)
                if prev is None or t.get("round", 0) >= prev.get("round", 0):
                    latest_by_role[role] = t
            for t in latest_by_role.values():
                committee.append({
                    "member_name": t.get("name", t.get("role", "")),
                    "member_role": t.get("role", ""),
                    "result_json": {
                        "vote": t.get("vote", ""),
                        "confidence": t.get("confidence"),
                        "overall_assessment": t.get("content", ""),
                        "key_concerns": t.get("key_concerns", []),
                        "error": t.get("error", ""),
                    },
                })
            # 回退：transcript_json 特性之前的旧会议记录 transcript=[]，
            # 但 committee_reviews 行仍有真实投票，按 execution_plan_id 重建面板
            if not committee:
                exec_plan_id = rec.get("execution_plan_id", "")
                if exec_plan_id:
                    try:
                        for rv in store.get_reviews_for_execution(exec_plan_id):
                            rj = rv.get("result_json") or {}
                            committee.append({
                                "member_name": rj.get("name", rv.get("member_role", "")),
                                "member_role": rv.get("member_role", ""),
                                "result_json": {
                                    "vote": rv.get("vote", rj.get("vote", "")),
                                    "confidence": rv.get("confidence", rj.get("confidence")),
                                    "overall_assessment": rj.get("overall_assessment", ""),
                                    "key_concerns": rv.get("key_concerns", rj.get("key_concerns", [])),
                                    "error": rj.get("error", ""),
                                },
                            })
                    except Exception:
                        logger.debug("回退读取投委会评审失败", exc_info=True)
            tickers = rec.get("tickers_discussed", []) or []
            consensus = rec.get("result_json", {})
            consensus = _maybe_json(consensus) or {}
            committee_meta = {
                "ticker": tickers[0] if tickers else "",
                "verdict": rec.get("final_verdict", ""),
                "created_at": rec.get("created_at", ""),
                "meeting_id": rec.get("id", ""),
                # 集体结论详情
                "approval_rate": consensus.get("approval_rate"),
                "summary": consensus.get("summary", ""),
                "modifications": consensus.get("consensus_modifications", []),
                "risks": consensus.get("key_risks_flagged", []),
                "minority": consensus.get("minority_opinions", []),
            }
    except Exception:
        logger.debug("加载投委会概览失败", exc_info=True)

    return {
        "macro_strategy": macro,
        "strategic_plan": strategic,
        "tactical_plans": tactical_plans,
        "pending_executions": pending,
        "upcoming_catalysts": catalysts[:10],
        "account": account,
        "positions": positions,
        "committee": committee,
        "committee_meta": committee_meta,
    }


@router.get("/scheduler-status")
async def get_scheduler_status(user: dict = Depends(get_current_user)):
    """返回决策自动调度任务的运行状态"""
    from bottleneck_hunter.watchlist.scheduler import get_job_statuses
    return {"jobs": get_job_statuses()}


# ─────────────────────────────────────────────────────────
# 17F.1 决策链路追溯
# ─────────────────────────────────────────────────────────

@router.get("/trace/{ticker}")
async def get_decision_trace(ticker: str, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """聚合一个 ticker 的完整 L1→L2→L3→L4→投委会 决策路径"""
    store = _user_store(user).for_market(market)

    layers = []

    # L1 宏观
    macro = store.get_latest_macro_strategy()
    if macro:
        rj = macro.get("result_json") or {}
        rj = _maybe_json(rj)
        layers.append({
            "level": "L1",
            "label": "宏观环境",
            "summary": rj.get("market_summary") or macro.get("market_summary") or "",
            "detail": {
                "regime": macro.get("regime", ""),
                "risk_appetite": macro.get("risk_appetite", ""),
                "cash_pct": macro.get("recommended_cash_pct"),
            },
            "updated_at": macro.get("created_at", ""),
        })

    # L2 组合 — 找出该 ticker 在战略计划中的目标权重
    strategic = store.get_latest_strategic_plan()
    if strategic:
        rj = strategic.get("result_json") or {}
        rj = _maybe_json(rj)
        target_alloc = rj.get("target_allocation", [])
        ticker_alloc = None
        if isinstance(target_alloc, list):
            for a in target_alloc:
                if a.get("ticker", "").upper() == ticker.upper():
                    ticker_alloc = a
                    break
        elif isinstance(target_alloc, dict):
            ticker_alloc = target_alloc.get(ticker) or target_alloc.get(ticker.upper())

        alloc_summary = ""
        alloc_detail = {}
        if ticker_alloc:
            weight = ticker_alloc.get("weight", 0)
            action = ticker_alloc.get("action", "")
            alloc_summary = f"目标权重 {round(weight * 100 if weight < 1 else weight)}%"
            if action:
                alloc_summary += f"，操作: {action}"
            alloc_detail = ticker_alloc
        else:
            alloc_summary = "未纳入组合目标"

        layers.append({
            "level": "L2",
            "label": "组合策略",
            "summary": alloc_summary,
            "detail": alloc_detail,
            "updated_at": strategic.get("created_at", ""),
        })

    # L3 战术
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).strftime("%Y-%m-%d")
    tactical = store.get_tactical_plan_for_ticker(ticker, today)
    if tactical:
        rj = tactical.get("result_json") or {}
        rj = _maybe_json(rj)
        action = rj.get("action") or tactical.get("action", "hold")
        entry_plan = rj.get("entry_plan") or {}
        exit_plan = rj.get("exit_plan") or {}
        confidence = rj.get("confidence") or tactical.get("confidence", 5)
        summary_parts = [f"操作: {action}"]
        if entry_plan.get("ideal_price"):
            summary_parts.append(f"入场价 {entry_plan['ideal_price']}")
        if exit_plan.get("stop_loss", {}).get("price"):
            summary_parts.append(f"止损 {exit_plan['stop_loss']['price']}")
        summary_parts.append(f"置信度 {confidence}/10")
        layers.append({
            "level": "L3",
            "label": "战术计划",
            "summary": "，".join(summary_parts),
            "detail": rj,
            "updated_at": tactical.get("created_at", ""),
        })

    # L4 执行 — 查找该 ticker 最新的执行计划
    conn = store._connect()
    try:
        q, p = store._user_filter(
            "SELECT * FROM execution_plans WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
            (ticker,),
        )
        ex_row = conn.execute(q, p).fetchone()
    finally:
        conn.close()
    if ex_row:
        ex = dict(ex_row)
        rj = ex.get("result_json")
        rj = _maybe_json(rj)
        rj = rj or {}
        action = ex.get("action", "hold")
        status = ex.get("status", "pending")
        shares = ex.get("shares", 0)
        price = ex.get("target_price")
        summary = f"操作: {action}，状态: {status}"
        if shares:
            summary += f"，{shares}股"
        if price:
            summary += f" @ {price}"
        layers.append({
            "level": "L4",
            "label": "执行方案",
            "summary": summary,
            "detail": {"action": action, "status": status, "shares": shares,
                       "target_price": price, "reasoning": ex.get("reasoning", "")},
            "updated_at": ex.get("created_at", ""),
        })

        # 投委会 — 查找该执行计划的共识
        conn = store._connect()
        try:
            q2, p2 = store._user_filter(
                "SELECT * FROM committee_consensus WHERE execution_plan_id = ? ORDER BY created_at DESC LIMIT 1",
                (ex["id"],),
            )
            consensus_row = conn.execute(q2, p2).fetchone()
        finally:
            conn.close()
        if consensus_row:
            c = dict(consensus_row)
            verdict = c.get("final_verdict", "")
            rate = c.get("approval_rate", 0)
            summary_c = f"{verdict}（通过率 {round(rate * 100 if rate <= 1 else rate)}%）"
            layers.append({
                "level": "投委会",
                "label": "投委会评审",
                "summary": summary_c,
                "detail": {"verdict": verdict, "approval_rate": rate,
                           "summary": c.get("summary", "")},
                "updated_at": c.get("created_at", ""),
            })

    return {"ticker": ticker, "layers": layers}


# ─────────────────────────────────────────────────────────
# 17F.2 风险仪表盘
# ─────────────────────────────────────────────────────────

@router.get("/risk-dashboard")
async def get_risk_dashboard(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """组合风险仪表盘 — VaR / CVaR / Beta / HHI + 持仓权重 + 预警"""
    store = _user_store(user).for_market(market)
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    total_equity = account.get("total_equity", 100000.0) or 100000.0

    if not positions:
        return {
            "var_95": 0, "cvar_95": 0, "portfolio_beta": 0,
            "concentration_index": 0, "weights": [], "correlation_pairs": [],
            "warnings": ["暂无持仓"],
        }

    # 获取每个持仓的历史价格
    price_histories = {}
    for pos in positions:
        ticker = pos.get("ticker", "")
        snapshots = store.get_snapshots(ticker, days=60)
        if snapshots:
            prices = [s["close"] for s in reversed(snapshots) if s.get("close")]
            if prices:
                price_histories[ticker] = prices

    from bottleneck_hunter.watchlist.risk_metrics import compute_portfolio_risk
    metrics = compute_portfolio_risk(
        positions=positions,
        price_histories=price_histories,
        total_equity=total_equity,
    )

    # 构建权重列表（饼图数据）
    weights = []
    for pos in positions:
        w = pos.get("weight_pct", 0)
        if w <= 0 and total_equity > 0:
            w = round((pos.get("market_value", 0) / total_equity) * 100, 2)
        weights.append({
            "ticker": pos.get("ticker", ""),
            "weight_pct": round(w, 2),
            "market_value": pos.get("market_value", 0),
        })
    cash = account.get("cash_balance", 0)
    cash_pct = round((cash / total_equity) * 100, 2) if total_equity > 0 else 0
    if cash_pct > 0:
        weights.append({"ticker": "现金", "weight_pct": cash_pct, "market_value": cash})

    return {
        "var_95": metrics.var_95,
        "cvar_95": metrics.cvar_95,
        "portfolio_beta": metrics.portfolio_beta,
        "concentration_index": metrics.concentration_index,
        "max_single_weight": metrics.max_single_weight,
        "max_sector_weight": metrics.max_sector_weight,
        "weights": weights,
        "correlation_pairs": metrics.correlation_pairs,
        "warnings": metrics.warnings,
    }


# ─────────────────────────────────────────────────────────
# 17F.4 A/B 对比
# ─────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────
# Phase 20A: 投资论点追踪
# ─────────────────────────────────────────────────────────

@router.post("/thesis/review")
async def trigger_thesis_review(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    """触发全量论点审查（SSE 流）"""
    from bottleneck_hunter.watchlist.thesis_tracker import run_quarterly_review
    store = _user_store(user).for_market(market)
    return _sse_response(request, run_quarterly_review(store, _user_budget(user)))


@router.get("/thesis/dashboard")
async def thesis_dashboard(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """论点健康度总览"""
    store = _user_store(user).for_market(market)
    dashboard = store.get_thesis_dashboard()
    return {"dashboard": dashboard}


@router.get("/thesis/{entry_id}")
async def get_theses(entry_id: str, active_only: bool = True, user: dict = Depends(get_current_user)):
    """获取某标的所有论点"""
    store = _user_store(user)
    theses = store.get_theses_for_entry(entry_id, active_only=active_only)
    return {"theses": theses}


class CreateThesisRequest(BaseModel):
    title: str
    summary: str = ""
    conviction: str = "medium"
    time_horizon: str = "medium_term"
    pillars: list[dict] = []


@router.post("/thesis/{entry_id}/create")
async def create_thesis(entry_id: str, req: CreateThesisRequest, user: dict = Depends(get_current_user)):
    """手动创建投资论点"""
    store = _user_store(user)
    entry = store.get(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="标的不存在")
    thesis_id = store.create_thesis(
        entry_id=entry_id,
        ticker=entry["ticker"],
        title=req.title,
        summary=req.summary,
        conviction=req.conviction,
        time_horizon=req.time_horizon,
        pillars=req.pillars or [{"text": req.summary[:100], "falsification": "待补充", "weight": 1.0}],
    )
    return {"thesis_id": thesis_id}


@router.get("/thesis/{thesis_id}/detail")
async def get_thesis_detail(thesis_id: str, user: dict = Depends(get_current_user)):
    """论点详情 + 支柱 + 证据日志"""
    store = _user_store(user)
    thesis = store.get_thesis(thesis_id)
    if not thesis:
        raise HTTPException(status_code=404, detail="论点不存在")
    pillars = store.get_pillars(thesis_id)
    evidence = store.get_evidence_log(thesis_id, limit=50)
    return {"thesis": thesis, "pillars": pillars, "evidence": evidence}


class AddEvidenceRequest(BaseModel):
    pillar_id: str | None = None
    data_point: str
    direction: str = "neutral"
    thesis_impact: str = "no_change"
    source: str = "manual"


@router.post("/thesis/{thesis_id}/evidence")
async def add_evidence(thesis_id: str, req: AddEvidenceRequest, user: dict = Depends(get_current_user)):
    """手动添加证据"""
    store = _user_store(user)
    thesis = store.get_thesis(thesis_id)
    if not thesis:
        raise HTTPException(status_code=404, detail="论点不存在")
    from datetime import datetime, timezone
    evidence_id = store.create_evidence(
        thesis_id=thesis_id,
        pillar_id=req.pillar_id,
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        data_point=req.data_point,
        direction=req.direction,
        thesis_impact=req.thesis_impact,
        conviction_before=thesis.get("conviction", "medium"),
        conviction_after=thesis.get("conviction", "medium"),
        source=req.source,
    )
    return {"evidence_id": evidence_id}


# ─────────────────────────────────────────────────────────
# Phase 20D: 三场景估值
# ─────────────────────────────────────────────────────────

@router.get("/valuation/portfolio/overview")
async def get_portfolio_valuations(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """组合级场景估值概览"""
    store = _user_store(user).for_market(market)
    valuations = store.get_portfolio_valuations()
    return {"valuations": valuations}


@router.get("/valuation/{entry_id}")
async def get_latest_valuation(entry_id: str, user: dict = Depends(get_current_user)):
    """最新场景估值"""
    store = _user_store(user)
    val = store.get_latest_valuation(entry_id)
    if not val:
        return {"valuation": None, "message": "暂无场景估值"}
    return {"valuation": val}


# ─────────────────────────────────────────────────────────
# Phase 22A: AI 模型评分校准
# ─────────────────────────────────────────────────────────

@router.get("/model-accuracy")
async def get_model_accuracy_overview(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """模型预测准确率总览 — 按 provider/model/role 分组统计"""
    store = _user_store(user)
    stats = store.get_model_accuracy_stats(market=market)
    ratings = store.get_model_ratings(market=market)
    return {"stats": stats, "ratings": ratings}


@router.get("/model-accuracy/{provider}/{model}")
async def get_model_accuracy_detail(provider: str, model: str,
                                    role_context: str | None = None,
                                    limit: int = 50,
                                    user: dict = Depends(get_current_user)):
    """单个模型的预测记录明细"""
    store = _user_store(user)
    records = store.get_model_accuracy(provider, model, role_context=role_context, limit=limit)
    weight = store.get_calibration_weight(provider, model, role_context=role_context or "")
    return {"records": records, "calibration_weight": weight}


@router.post("/model-accuracy/calibrate")
async def calibrate_models(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """手动触发模型校准 — 委托 ModelCalibrator 统一计算"""
    from bottleneck_hunter.watchlist.model_calibrator import ModelCalibrator
    store = _user_store(user)
    calibrator = ModelCalibrator(store)
    calibrated = calibrator.recalibrate(market=market)
    return {"calibrated": calibrated, "message": f"已校准 {calibrated} 个模型/角色组合"}


# ── 模型调度用量看板 + 用户策略（智能调度 Phase 2）──────────────

@router.get("/model-usage")
async def get_model_usage(days: int = 14, user: dict = Depends(get_current_user)):
    """模型调度看板：各模型调用/成功率/延迟 + 长期曲线 + 当前熔断 + 用户策略。"""
    store = _user_store(user)
    uid = user.get("sub", "")
    stats = store.get_model_call_stats(days=days)
    series = store.get_model_call_series(days=days)
    from bottleneck_hunter.llm_clients.health import health, provider_tier
    for r in stats:
        r["tier"] = provider_tier(r["provider"])
        r["cooldown_s"] = health.cooldown_remaining(uid, r["provider"])
    cooling = [{"provider": r["provider"], "cooldown_s": r["cooldown_s"]}
               for r in stats if r["cooldown_s"] > 0]
    policy = store.get_routing_policy() or {"prefer_tier": "auto", "optimize_for": "balanced"}
    return {"stats": stats, "series": series, "cooling": cooling, "policy": policy}


class RoutingPolicyRequest(BaseModel):
    prefer_tier: str = "auto"       # auto | free | paid
    optimize_for: str = "balanced"  # balanced | quality | price
    role_key: str = ""              # 空=全局默认；非空=角色覆盖


@router.get("/routing-policy")
async def get_routing_policy_ep(user: dict = Depends(get_current_user)):
    """返回当前用户的全局策略 + 所有角色覆盖。"""
    store = _user_store(user)
    return {
        "global": store.get_routing_policy(role_key="") or {"prefer_tier": "auto", "optimize_for": "balanced"},
        "overrides": store.get_all_routing_policies(),
    }


@router.post("/routing-policy")
async def set_routing_policy_ep(req: RoutingPolicyRequest, user: dict = Depends(get_current_user)):
    """保存用户模型策略（严格按用户隔离）。role_key 空=全局默认。"""
    if req.prefer_tier not in ("auto", "free", "paid"):
        raise HTTPException(400, "prefer_tier 无效")
    if req.optimize_for not in ("balanced", "quality", "price"):
        raise HTTPException(400, "optimize_for 无效")
    store = _user_store(user)
    store.set_routing_policy(req.prefer_tier, req.optimize_for, role_key=req.role_key)
    return {"ok": True}


# ─────────────────────────────────────────────────────────
# Phase 22A: 会议历史记录
# ─────────────────────────────────────────────────────────

@router.get("/meetings")
async def get_meetings(meeting_type: str | None = None, market: str = "us_stock",
                       limit: int = 20, user: dict = Depends(get_current_user)):
    """会议历史列表"""
    store = _user_store(user)
    records = store.get_meeting_records(meeting_type=meeting_type, market=market, limit=limit)
    # 宏观咨询是聊天会话（有独立抽屉），不混入正式会议历史列表
    if meeting_type is None:
        records = [r for r in records if r.get("meeting_type") != "macro_consult"]
    return {"meetings": records}


@router.get("/meetings/stats")
async def get_meetings_stats(market: str = "us_stock", user: dict = Depends(get_current_user)):
    """会议统计概览"""
    store = _user_store(user)
    stats = store.get_meeting_stats(market=market)
    return {"stats": stats}


@router.get("/meetings/{record_id}")
async def get_meeting_detail(record_id: str, user: dict = Depends(get_current_user)):
    """单条会议详情"""
    store = _user_store(user)
    record = store.get_meeting_record(record_id)
    if not record:
        raise HTTPException(status_code=404, detail="会议记录不存在")
    return {"meeting": record}


class MeetingOutcomeRequest(BaseModel):
    outcome_summary: str


@router.post("/meetings/{record_id}/outcome")
async def update_meeting_outcome(record_id: str, req: MeetingOutcomeRequest,
                                 user: dict = Depends(get_current_user)):
    """更新会议回溯结论"""
    store = _user_store(user)
    ok = store.update_meeting_outcome(record_id, req.outcome_summary)
    if not ok:
        raise HTTPException(status_code=404, detail="会议记录不存在")
    return {"status": "updated"}
