"""决策中心 API — 挂载于 /api/decision

提供 L1-L2 决策引擎、催化剂管理、模拟账户等端点。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.watchlist.store import WatchlistStore

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
async def generate_macro_strategy(request: Request):
    """全面生成 L1 宏观策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_strategy
    return _sse_response(request, run_macro_strategy(_get_store(), _budget()))


@router.post("/macro/check")
async def check_macro_strategy(request: Request):
    """L1 日常检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_macro_check
    return _sse_response(request, run_macro_check(_get_store(), _budget()))


@router.get("/macro/latest")
async def get_latest_macro():
    """获取最新 L1 宏观策略"""
    store = _get_store()
    strategy = store.get_latest_macro_strategy()
    if not strategy:
        return {"strategy": None, "message": "尚未生成宏观策略"}
    return {"strategy": strategy}


@router.get("/macro/history")
async def get_macro_history(limit: int = 10):
    store = _get_store()
    return {"history": store.get_macro_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# L2 组合策略
# ─────────────────────────────────────────────────────────

@router.post("/strategic/generate")
async def generate_strategic_plan(request: Request):
    """全面生成 L2 组合策略（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_strategic_plan
    return _sse_response(request, run_strategic_plan(_get_store(), _budget()))


@router.post("/strategic/deviation-check")
async def deviation_check(request: Request):
    """L2 偏离检查（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_deviation_check
    return _sse_response(request, run_deviation_check(_get_store(), _budget()))


@router.get("/strategic/latest")
async def get_latest_strategic():
    store = _get_store()
    plan = store.get_latest_strategic_plan()
    if not plan:
        return {"plan": None, "message": "尚未生成组合策略"}
    return {"plan": plan}


@router.get("/strategic/history")
async def get_strategic_history(limit: int = 10):
    store = _get_store()
    return {"history": store.get_strategic_history(limit=limit)}


# ─────────────────────────────────────────────────────────
# 日常决策流程
# ─────────────────────────────────────────────────────────

class DailyRequest(BaseModel):
    scope: str = "full"


@router.post("/daily")
async def run_daily(request: Request, body: DailyRequest | None = None):
    """执行日常决策流程 L1→L2→L3→L4→投委会（SSE 流）

    scope: "full" | "l1" | "l3l4"
    """
    from bottleneck_hunter.watchlist.decision_engine import run_daily_decision
    scope = body.scope if body else "full"
    return _sse_response(request, run_daily_decision(_get_store(), _budget(), scope=scope))


@router.post("/full-refresh")
async def full_refresh(request: Request):
    """全量刷新 L1+L2+L3+L4+投委会（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_full_refresh
    return _sse_response(request, run_full_refresh(_get_store(), _budget()))


# ─────────────────────────────────────────────────────────
# L3 战术计划
# ─────────────────────────────────────────────────────────

@router.post("/tactical/generate")
async def generate_tactical(request: Request):
    """生成 L3 战术计划（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_tactical_plans
    return _sse_response(request, run_tactical_plans(_get_store(), _budget()))


@router.get("/tactical/latest")
async def get_latest_tactical():
    store = _get_store()
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plans = store.get_tactical_plans_by_date(today)
    if not plans:
        return {"plans": [], "message": "今日尚未生成战术计划"}
    return {"plans": plans}


@router.get("/tactical/{ticker}")
async def get_tactical_for_ticker(ticker: str):
    store = _get_store()
    plan = store.get_tactical_plan_for_ticker(ticker)
    if not plan:
        return {"plan": None, "message": f"{ticker} 无战术计划"}
    return {"plan": plan}


# ─────────────────────────────────────────────────────────
# L4 执行方案
# ─────────────────────────────────────────────────────────

@router.post("/execution/generate")
async def generate_execution(request: Request):
    """生成 L4 执行方案（SSE 流）"""
    from bottleneck_hunter.watchlist.decision_engine import run_execution_plans
    return _sse_response(request, run_execution_plans(_get_store(), _budget()))


@router.get("/execution/{plan_id}")
async def get_execution_detail(plan_id: str):
    store = _get_store()
    plan = store.get_execution_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="执行计划不存在")
    reviews = store.get_reviews_for_execution(plan_id)
    return {"plan": plan, "reviews": reviews}


# ─────────────────────────────────────────────────────────
# 投委会
# ─────────────────────────────────────────────────────────

@router.post("/committee/review")
async def trigger_committee_review(request: Request):
    """对所有待审执行计划启动投委会评审（SSE 流）"""
    store = _get_store()
    pending = store.get_pending_executions()
    if not pending:
        return {"message": "无待审执行计划"}
    from bottleneck_hunter.watchlist.committee import run_committee_review
    return _sse_response(request, run_committee_review(store, pending, _budget()))


@router.get("/committee/reviews/{plan_id}")
async def get_committee_reviews(plan_id: str):
    store = _get_store()
    reviews = store.get_reviews_for_execution(plan_id)
    return {"reviews": reviews}


@router.get("/committee/consensus/{plan_id}")
async def get_committee_consensus(plan_id: str):
    store = _get_store()
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
async def scan_catalysts(request: Request):
    """扫描观察池提取催化剂（SSE 流）"""
    from bottleneck_hunter.watchlist.catalyst_monitor import detect_catalysts
    return _sse_response(request, detect_catalysts(_get_store(), _budget()))


@router.get("/catalysts/upcoming")
async def get_upcoming_catalysts(days: int = 14):
    store = _get_store()
    return {"catalysts": store.get_upcoming_catalysts(days=days)}


@router.get("/catalysts/{entry_id}")
async def get_entry_catalysts(entry_id: str, active_only: bool = True):
    store = _get_store()
    return {"catalysts": store.get_catalysts_for_entry(entry_id, active_only=active_only)}


class UpdateCatalystRequest(BaseModel):
    status: str
    outcome: str = ""
    actual_date: str | None = None


@router.patch("/catalysts/item/{catalyst_id}")
async def update_catalyst(catalyst_id: str, req: UpdateCatalystRequest):
    store = _get_store()
    ok = store.update_catalyst_status(catalyst_id, req.status, req.outcome, req.actual_date)
    if not ok:
        raise HTTPException(status_code=404, detail="催化剂不存在")
    return {"status": "updated"}


# ─────────────────────────────────────────────────────────
# 执行计划 (L4)
# ─────────────────────────────────────────────────────────

@router.get("/executions/pending")
async def get_pending_executions():
    store = _get_store()
    return {"executions": store.get_pending_executions()}


@router.post("/executions/{plan_id}/confirm")
async def confirm_execution(plan_id: str):
    store = _get_store()
    ok = store.confirm_execution(plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="执行计划不存在或已处理")
    from bottleneck_hunter.watchlist.trade_executor import execute_trade
    trade_result = execute_trade(store, plan_id)
    return {"status": "confirmed", "trade": trade_result}


class RejectRequest(BaseModel):
    reason: str = ""


@router.post("/executions/{plan_id}/reject")
async def reject_execution(plan_id: str, req: RejectRequest):
    store = _get_store()
    ok = store.reject_execution(plan_id, req.reason)
    if not ok:
        raise HTTPException(status_code=404, detail="执行计划不存在或已处理")
    return {"status": "rejected"}


# ─────────────────────────────────────────────────────────
# 模拟账户
# ─────────────────────────────────────────────────────────

@router.get("/account")
async def get_account():
    store = _get_store()
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    return {"account": account, "positions": positions}


@router.get("/account/equity-history")
async def get_equity_history(days: int = 30):
    """按日聚合交易记录，计算每日权益值"""
    store = _get_store()
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
async def get_trades(ticker: str | None = None, limit: int = 50):
    store = _get_store()
    return {"trades": store.get_sim_trades(ticker=ticker, limit=limit)}


# ─────────────────────────────────────────────────────────
# 策略概览（整合 L1+L2+L3+L4+投委会+催化剂）
# ─────────────────────────────────────────────────────────

@router.get("/overview")
async def decision_overview():
    """策略中心概览 — 一次请求拿到所有关键数据"""
    store = _get_store()

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
async def get_ai_config():
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
async def save_ai_config(req: AIConfigSaveRequest):
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
async def test_ai_model(req: AIConfigTestRequest):
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
async def get_reviews(ticker: str | None = None, limit: int = 20):
    store = _get_store()
    return {"reviews": store.get_auto_reviews(ticker=ticker, limit=limit)}


@router.get("/reviews/{review_id}")
async def get_review_detail(review_id: str):
    store = _get_store()
    review = store.get_auto_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="复盘记录不存在")
    return {"review": review}


@router.post("/reviews/run")
async def run_batch_review_endpoint(request: Request):
    store = _get_store()
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
async def get_feedback_history(limit: int = 50):
    store = _get_store()
    return {"feedback": store.get_trade_feedback_history(limit=limit)}


@router.get("/experience")
async def get_experience_cards(scope: str | None = None,
                               scope_key: str | None = None,
                               limit: int = 20):
    store = _get_store()
    return {"cards": store.get_experience_cards(scope=scope, scope_key=scope_key, limit=limit)}


@router.delete("/experience/{card_id}")
async def delete_experience_card(card_id: str):
    store = _get_store()
    ok = store.delete_experience_card(card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="经验卡片不存在")
    return {"status": "deleted"}


# ─────────────────────────────────────────────────────────
# 绩效统计
# ─────────────────────────────────────────────────────────

@router.get("/performance")
async def get_performance():
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_get_store())
    return {
        "overview": calc.compute_overview(),
        "drawdown": calc.compute_drawdown(),
        "cost": calc.compute_cost_summary(),
        "review_summary": calc.compute_review_summary(),
    }


@router.get("/performance/monthly")
async def get_performance_monthly(months: int = 6):
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_get_store())
    return {"monthly": calc.compute_monthly_series(months=months)}


@router.get("/performance/tickers")
async def get_performance_tickers():
    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(_get_store())
    return {"tickers": calc.compute_by_ticker()}


# ─────────────────────────────────────────────────────────
# 调优管理
# ─────────────────────────────────────────────────────────

@router.get("/tuning")
async def get_tuning(status: str | None = None, limit: int = 20):
    store = _get_store()
    return {"proposals": store.get_tuning_proposals(status=status, limit=limit)}


@router.post("/tuning/generate")
async def generate_tuning(request: Request):
    store = _get_store()
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
async def approve_tuning(tuning_id: str):
    store = _get_store()
    ok = store.approve_tuning(tuning_id)
    if not ok:
        raise HTTPException(status_code=404, detail="调优建议不存在或已处理")
    return {"status": "approved"}


@router.post("/tuning/{tuning_id}/reject")
async def reject_tuning(tuning_id: str, reason: str = ""):
    store = _get_store()
    ok = store.reject_tuning(tuning_id, reason)
    if not ok:
        raise HTTPException(status_code=404, detail="调优建议不存在或已处理")
    return {"status": "rejected"}
