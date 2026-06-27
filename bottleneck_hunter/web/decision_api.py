"""决策中心 API — 挂载于 /api/decision

提供 L1-L2 决策引擎、催化剂管理、模拟账户等端点。
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


def _user_budget(user: dict):
    """返回绑定当前用户的 BudgetTracker。"""
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    return BudgetTracker(_user_store(user))


def _budget():
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    return BudgetTracker(_get_store())


def _sse_response(request: Request, gen_coro):
    async def event_generator():
        async for evt in gen_coro:
            if await request.is_disconnected():
                break
            yield {
                "event": evt.get("event", "progress"),
                "data": json.dumps(evt.get("data", {}), ensure_ascii=False),
            }
    return EventSourceResponse(event_generator())


# ─────────────────────────────────────────────────────────
# L1 宏观策略
# ─────────────────────────────────────────────────────────

@router.post("/macro/generate")
async def generate_macro_strategy(request: Request, user: dict = Depends(get_current_user)):
    """全面生成 L1 宏观策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy
    store = _user_store(user)
    return _sse_response(request, run_macro_strategy(store, _user_budget(user)))


@router.post("/macro/check")
async def check_macro_strategy(request: Request, user: dict = Depends(get_current_user)):
    """L1 日常检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_check
    store = _user_store(user)
    return _sse_response(request, run_macro_check(store, _user_budget(user)))


@router.get("/macro/latest")
async def get_latest_macro(user: dict = Depends(get_current_user)):
    """获取最新 L1 宏观策略"""
    store = _user_store(user)
    strategy = store.get_latest_macro_strategy()
    if not strategy:
        return {"strategy": None, "message": "尚未生成宏观策略"}
    return {"strategy": strategy}


@router.get("/macro/history")
async def get_macro_history(limit: int = 10, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"history": store.get_macro_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# L2 组合策略
# ─────────────────────────────────────────────────────────

@router.post("/strategic/generate")
async def generate_strategic_plan(request: Request, user: dict = Depends(get_current_user)):
    """全面生成 L2 组合策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan
    store = _user_store(user)
    return _sse_response(request, run_strategic_plan(store, _user_budget(user)))


@router.post("/strategic/deviation-check")
async def deviation_check(request: Request, user: dict = Depends(get_current_user)):
    """L2 偏离检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_deviation_check
    store = _user_store(user)
    return _sse_response(request, run_deviation_check(store, _user_budget(user)))


@router.get("/strategic/latest")
async def get_latest_strategic(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    plan = store.get_latest_strategic_plan()
    if not plan:
        return {"plan": None, "message": "尚未生成组合策略"}
    return {"plan": plan}


@router.get("/strategic/history")
async def get_strategic_history(limit: int = 10, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"history": store.get_strategic_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# 日常决策流程
# ─────────────────────────────────────────────────────────

class DailyRequest(BaseModel):
    scope: str = "full"


@router.post("/daily")
async def run_daily(request: Request, body: DailyRequest | None = None, user: dict = Depends(get_current_user)):
    """执行日常决策流程 L1→L2→L3→L4→投委会（SSE 流）

    scope: "full" | "l1" | "l3l4"
    """
    from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
    store = _user_store(user)
    scope = body.scope if body else "full"
    return _sse_response(request, run_daily_decision(store, _user_budget(user), scope=scope))


@router.post("/full-refresh")
async def full_refresh(request: Request, user: dict = Depends(get_current_user)):
    """全量刷新 L1+L2+L3+L4+投委会（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_full_refresh
    store = _user_store(user)
    return _sse_response(request, run_full_refresh(store, _user_budget(user)))


# ─────────────────────────────────────────────────────────
# L3 战术计划
# ─────────────────────────────────────────────────────────

@router.post("/tactical/generate")
async def generate_tactical(request: Request, user: dict = Depends(get_current_user)):
    """生成 L3 战术计划（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
    store = _user_store(user)
    return _sse_response(request, run_tactical_plans(store, _user_budget(user)))


@router.get("/tactical/latest")
async def get_latest_tactical(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plans = store.get_tactical_plans_by_date(today)
    if not plans:
        return {"plans": [], "message": "今日尚未生成战术计划"}
    return {"plans": plans}


@router.get("/tactical/{ticker}")
async def get_tactical_for_ticker(ticker: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    plan = store.get_tactical_plan_for_ticker(ticker)
    if not plan:
        return {"plan": None, "message": f"{ticker} 无战术计划"}
    return {"plan": plan}


# ─────────────────────────────────────────────────────────
# L4 执行方案
# ─────────────────────────────────────────────────────────

@router.post("/execution/generate")
async def generate_execution(request: Request, user: dict = Depends(get_current_user)):
    """生成 L4 执行方案（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
    store = _user_store(user)
    return _sse_response(request, run_execution_plans(store, _user_budget(user)))


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
async def trigger_committee_review(request: Request, user: dict = Depends(get_current_user)):
    """对所有待审执行计划启动投委会评审（SSE 流）"""
    store = _user_store(user)
    pending = store.get_pending_executions()
    if not pending:
        return {"message": "无待审执行计划"}
    from bottleneck_hunter.watchlist.committee import run_committee_review
    return _sse_response(request, run_committee_review(store, pending, _user_budget(user)))


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
        row = conn.execute(
            "SELECT * FROM committee_consensus WHERE execution_plan_id = ? ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
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


# ─────────────────────────────────────────────────────────
# 催化剂
# ─────────────────────────────────────────────────────────

@router.post("/catalysts/scan")
async def scan_catalysts(request: Request, user: dict = Depends(get_current_user)):
    """扫描观察池提取催化剂（SSE 流）"""
    from bottleneck_hunter.watchlist.catalyst_monitor import detect_catalysts
    store = _user_store(user)
    return _sse_response(request, detect_catalysts(store, _user_budget(user)))


@router.get("/catalysts/upcoming")
async def get_upcoming_catalysts(days: int = 14, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"catalysts": store.get_upcoming_catalysts(days=days)}


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
async def get_pending_executions(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"executions": store.get_pending_executions()}


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
        raise HTTPException(status_code=500, detail=f"交易执行异常: {e}")
    # execute_trade 返回 error 字段表示业务错误（约束不通过、现金不足等）
    if "error" in trade_result:
        return {"status": "error", "trade": trade_result, "message": trade_result["error"]}
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


# ─────────────────────────────────────────────────────────
# 模拟账户
# ─────────────────────────────────────────────────────────

@router.get("/account")
async def get_account(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    return {"account": account, "positions": positions}


@router.get("/account/equity-history")
async def get_equity_history(days: int = 30, user: dict = Depends(get_current_user)):
    """按日聚合交易记录，计算每日权益值"""
    store = _user_store(user)
    account = store.get_sim_account()
    initial = account.get("initial_capital", 100000)
    trades = store.get_sim_trades(limit=10000)

    from collections import defaultdict
    daily_cash_flow = defaultdict(float)
    for t in trades:
        date = (t.get("created_at") or "")[:10]
        if not date:
            continue
        if t.get("side") == "buy":
            daily_cash_flow[date] -= t.get("amount", 0)
        else:
            daily_cash_flow[date] += t.get("amount", 0)

    if not daily_cash_flow:
        return {"history": [], "initial_capital": initial}

    sorted_dates = sorted(daily_cash_flow.keys())
    history = []
    equity = initial
    for d in sorted_dates:
        equity += daily_cash_flow[d]
        history.append({"date": d, "equity": round(equity, 2)})

    history = history[-days:]
    return {"history": history, "initial_capital": initial}


@router.get("/trades")
async def get_trades(ticker: str | None = None, limit: int = 50, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"trades": store.get_sim_trades(ticker=ticker, limit=limit)}


# ─────────────────────────────────────────────────────────
# 策略概览（整合 L1+L2+L3+L4+投委会+催化剂）
# ─────────────────────────────────────────────────────────

@router.get("/overview")
async def decision_overview(user: dict = Depends(get_current_user)):
    """策略中心概览 — 一次请求拿到所有关键数据"""
    store = _user_store(user)

    macro = store.get_latest_macro_strategy()
    strategic = store.get_latest_strategic_plan()
    pending = store.get_pending_executions()
    catalysts = store.get_upcoming_catalysts()
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tactical_plans = store.get_tactical_plans_by_date(today)

    return {
        "macro_strategy": macro,
        "strategic_plan": strategic,
        "tactical_plans": tactical_plans,
        "pending_executions": pending,
        "upcoming_catalysts": catalysts[:10],
        "account": account,
        "positions": positions,
    }


@router.get("/scheduler-status")
async def get_scheduler_status(user: dict = Depends(get_current_user)):
    """返回决策自动调度任务的运行状态"""
    from bottleneck_hunter.watchlist.scheduler import get_job_statuses
    return {"jobs": get_job_statuses()}


# ─────────────────────────────────────────────────────────
# AI 模型配置
# ─────────────────────────────────────────────────────────

AI_CONFIG_POSITIONS = [
    {"key": "L1_macro", "label": "L1 宏观策略", "group": "decision", "default_provider": "deepseek", "default_model": "deepseek-chat"},
    {"key": "L2_strategic", "label": "L2 组合策略", "group": "decision", "default_provider": "deepseek", "default_model": "deepseek-chat"},
    {"key": "L3_tactical", "label": "L3 战术计划", "group": "decision", "default_provider": "deepseek", "default_model": "deepseek-chat"},
    {"key": "L4_execution", "label": "L4 执行方案", "group": "decision", "default_provider": "deepseek", "default_model": "deepseek-chat"},
    {"key": "committee_risk", "label": "风险控制官", "group": "committee", "default_provider": "deepseek", "default_model": "deepseek-chat"},
    {"key": "committee_growth", "label": "成长投资人", "group": "committee", "default_provider": "qwen", "default_model": "qwen-plus"},
    {"key": "committee_value", "label": "价值投资人", "group": "committee", "default_provider": "kimi", "default_model": "moonshot-v1-8k"},
    {"key": "committee_contrarian", "label": "逆向投资人", "group": "committee", "default_provider": "glm", "default_model": "glm-4-flash"},
    {"key": "committee_consensus", "label": "圆桌讨论/共识", "group": "committee", "default_provider": "deepseek", "default_model": "deepseek-chat"},
]

PROVIDER_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-6",
    "deepseek": "deepseek-chat",
    "google": "gemini-2.5-flash",
    "qwen": "qwen-plus",
    "glm": "glm-4-flash",
    "minimax": "MiniMax-Text-01",
    "openrouter": "deepseek/deepseek-chat",
    "siliconflow": "deepseek-ai/DeepSeek-V3",
    "agnes": "agnes-2.0-flash",
    "kimi": "moonshot-v1-8k",
}


@router.get("/ai-config")
async def get_ai_config(user: dict = Depends(get_current_user)):
    """返回 9 个位置的当前 AI 模型配置 + 可用 provider 列表"""
    from bottleneck_hunter.web.api import PROVIDER_REGISTRY

    positions = []
    for pos in AI_CONFIG_POSITIONS:
        env_key = f"DC_MODEL_{pos['key'].upper()}"
        env_val = os.environ.get(env_key, "").strip()
        configured_provider = ""
        configured_model = ""
        if env_val and ":" in env_val:
            configured_provider, configured_model = env_val.split(":", 1)
        positions.append({
            "key": pos["key"],
            "label": pos["label"],
            "group": pos["group"],
            "default_provider": pos["default_provider"],
            "default_model": pos["default_model"],
            "configured_provider": configured_provider,
            "configured_model": configured_model,
        })

    available_providers = []
    for p in PROVIDER_REGISTRY:
        env_var = p["env_var"]
        has_key = bool(os.environ.get(env_var, "").strip())
        available_providers.append({
            "id": p["id"],
            "name": p["name"],
            "configured": has_key,
            "default_model": PROVIDER_MODELS.get(p["id"], ""),
        })

    return {"positions": positions, "available_providers": available_providers}


class AIConfigSaveRequest(BaseModel):
    configs: dict[str, str]


@router.post("/ai-config")
async def save_ai_config(req: AIConfigSaveRequest, user: dict = Depends(get_current_user)):
    """保存 AI 模型配置到 .env"""
    from pathlib import Path
    from dotenv import set_key

    env_path = Path.cwd() / ".env"
    valid_keys = {pos["key"] for pos in AI_CONFIG_POSITIONS}

    for key, value in req.configs.items():
        if key not in valid_keys:
            continue
        env_key = f"DC_MODEL_{key.upper()}"
        if value and ":" in value:
            set_key(str(env_path), env_key, value)
            os.environ[env_key] = value
        else:
            set_key(str(env_path), env_key, "")
            os.environ.pop(env_key, None)

    return {"status": "saved", "message": "AI 模型配置已保存"}


class AIConfigTestRequest(BaseModel):
    provider: str
    model: str


@router.post("/ai-config/test")
async def test_ai_model(req: AIConfigTestRequest, user: dict = Depends(get_current_user)):
    """测试单个 AI 模型连通性"""
    from langchain_core.messages import HumanMessage
    from bottleneck_hunter.llm_clients.factory import create_llm

    try:
        llm = create_llm(req.provider, req.model)
        await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="hi")]),
            timeout=30,
        )
        return {"success": True, "provider": req.provider, "model": req.model}
    except asyncio.TimeoutError:
        return {"success": False, "error": "请求超时（30s）", "provider": req.provider, "model": req.model}
    except Exception as e:
        err_msg = str(e)
        if len(err_msg) > 120:
            err_msg = err_msg[:120] + "..."
        return {"success": False, "error": err_msg, "provider": req.provider, "model": req.model}


# ─────────────────────────────────────────────────────────
# 交易复盘 & 经验卡片
# ─────────────────────────────────────────────────────────

@router.get("/reviews")
async def get_reviews(ticker: str | None = None, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"reviews": store.get_auto_reviews(ticker=ticker, limit=limit)}


@router.get("/reviews/{review_id}")
async def get_review_detail(review_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    review = store.get_auto_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return {"review": review}


@router.post("/reviews/run")
async def run_batch_review_endpoint(request: Request, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    budget = BudgetTracker(store)

    from bottleneck_hunter.watchlist.trade_reviewer import run_batch_review

    async def generate():
        async for evt in run_batch_review(store, budget):
            event_name = evt.get("event", "message")
            data = json.dumps(evt.get("data", evt), ensure_ascii=False)
            yield {"event": event_name, "data": data}

    return EventSourceResponse(generate())


@router.get("/feedback")
async def get_feedback_history(limit: int = 50, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"feedback": store.get_trade_feedback_history(limit=limit)}


@router.get("/experience")
async def get_experience_cards(scope: str | None = None,
                               scope_key: str | None = None,
                               limit: int = 20,
                               user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"cards": store.get_experience_cards(scope=scope, scope_key=scope_key, limit=limit)}


@router.delete("/experience/{card_id}")
async def delete_experience_card(card_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.delete_experience_card(card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="经验卡片不存在")
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────
# 绩效统计
# ─────────────────────────────────────────────────────────

@router.get("/performance")
async def get_performance(user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_user_store(user))
    return {
        "overview": calc.compute_overview(),
        "drawdown": calc.compute_drawdown(),
        "cost": calc.compute_cost_summary(),
        "review_summary": calc.compute_review_summary(),
    }


@router.get("/performance/monthly")
async def get_performance_monthly(months: int = 6, user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_user_store(user))
    return {"monthly": calc.compute_monthly_series(months=months)}


@router.get("/performance/tickers")
async def get_performance_tickers(user: dict = Depends(get_current_user)):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_user_store(user))
    return {"tickers": calc.compute_by_ticker()}


# ─────────────────────────────────────────────────────────
# 调优管理
# ─────────────────────────────────────────────────────────

@router.get("/tuning")
async def get_tuning(status: str | None = None, limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"proposals": store.get_tuning_proposals(status=status, limit=limit)}


@router.post("/tuning/generate")
async def generate_tuning(request: Request, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    budget = BudgetTracker(store)

    from bottleneck_hunter.watchlist.tuning_engine import generate_tuning_suggestions

    async def generate():
        async for evt in generate_tuning_suggestions(store, budget):
            event_name = evt.get("event", "message")
            data = json.dumps(evt.get("data", evt), ensure_ascii=False)
            yield {"event": event_name, "data": data}

    return EventSourceResponse(generate())


@router.post("/tuning/{tuning_id}/approve")
async def approve_tuning(tuning_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.approve_tuning(tuning_id)
    if not ok:
        raise HTTPException(status_code=404, detail="调优建议不存在或已处理")
    return {"status": "approved"}


@router.post("/tuning/{tuning_id}/reject")
async def reject_tuning(tuning_id: str, reason: str = "", user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.reject_tuning(tuning_id, reason)
    if not ok:
        raise HTTPException(status_code=404, detail="调优建议不存在或已处理")
    return {"status": "rejected"}


# ------------------------------------------------------------------
# Backtest
# ------------------------------------------------------------------

class BacktestRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    benchmark: str = "SPY"


@router.post("/backtest/run")
async def run_backtest(req: BacktestRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    from bottleneck_hunter.watchlist.backtest import BacktestEngine
    engine = BacktestEngine(store)
    result = engine.run(req.start_date, req.end_date, req.benchmark)
    if result.error:
        return {"error": result.error}
    return {
        "run_id": result.run_id,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "trade_count": result.trade_count,
        "metrics": {
            "total_return_pct": result.metrics.total_return_pct,
            "annualized_return_pct": result.metrics.annualized_return_pct,
            "sharpe_ratio": result.metrics.sharpe_ratio,
            "sortino_ratio": result.metrics.sortino_ratio,
            "max_drawdown_pct": result.metrics.max_drawdown_pct,
            "calmar_ratio": result.metrics.calmar_ratio,
            "win_rate_pct": result.metrics.win_rate_pct,
            "profit_loss_ratio": result.metrics.profit_loss_ratio,
            "benchmark_return_pct": result.metrics.benchmark_return_pct,
            "alpha_pct": result.metrics.alpha_pct,
        },
        "equity_curve": result.metrics.equity_curve,
    }


@router.get("/backtest/history")
async def backtest_history(limit: int = 20, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    runs = store.get_backtest_runs(limit)
    for r in runs:
        r.pop("equity_curve", None)
    return runs


@router.get("/backtest/{run_id}")
async def get_backtest(run_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    run = store.get_backtest_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="回测记录不存在")
    return run


# ─────────────────────────────────────────────────────────
# 17F.1 决策链路追溯
# ─────────────────────────────────────────────────────────

@router.get("/trace/{ticker}")
async def get_decision_trace(ticker: str, user: dict = Depends(get_current_user)):
    """聚合一个 ticker 的完整 L1→L2→L3→L4→投委会 决策路径"""
    store = _user_store(user)

    layers = []

    # L1 宏观
    macro = store.get_latest_macro_strategy()
    if macro:
        rj = macro.get("result_json") or {}
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = {}
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
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = {}
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
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = {}
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
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = {}
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
async def get_risk_dashboard(user: dict = Depends(get_current_user)):
    """组合风险仪表盘 — VaR / CVaR / Beta / HHI + 持仓权重 + 预警"""
    store = _user_store(user)
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
# 17F.3 催化剂日历视图
# ─────────────────────────────────────────────────────────

@router.get("/catalysts/calendar")
async def get_catalysts_calendar(month: str | None = None, user: dict = Depends(get_current_user)):
    """按日期分组返回催化剂事件，格式：{"2026-06-24": [...], ...}"""
    store = _user_store(user)
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
        q, p = store._user_filter(
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


# ─────────────────────────────────────────────────────────
# 17F.4 A/B 对比
# ─────────────────────────────────────────────────────────

class SnapshotRequest(BaseModel):
    label: str = ""


@router.post("/compare/snapshot")
async def create_snapshot(req: SnapshotRequest, user: dict = Depends(get_current_user)):
    """保存当前配置参数快照"""
    from bottleneck_hunter.watchlist.ab_compare import ABCompare
    store = _user_store(user)
    ab = ABCompare(store)
    params = ab.get_current_params()
    label = req.label or f"快照 {len(ab.list_snapshots()) + 1}"
    snapshot_id = ab.snapshot_params(label, params)
    return {"snapshot_id": snapshot_id, "label": label, "params_count": len(params)}


@router.get("/compare/snapshots")
async def list_snapshots(user: dict = Depends(get_current_user)):
    """列出所有参数快照"""
    from bottleneck_hunter.watchlist.ab_compare import ABCompare
    store = _user_store(user)
    ab = ABCompare(store)
    return {"snapshots": ab.list_snapshots()}


@router.get("/compare/{id_a}/{id_b}")
async def compare_snapshots(id_a: str, id_b: str, user: dict = Depends(get_current_user)):
    """对比两个参数快照的差异"""
    from bottleneck_hunter.watchlist.ab_compare import ABCompare
    store = _user_store(user)
    ab = ABCompare(store)
    result = ab.compare(id_a, id_b)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
