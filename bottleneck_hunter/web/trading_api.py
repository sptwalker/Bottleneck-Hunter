"""模拟交易 API — 挂载于 /api/trading

从决策中心独立出来的交易模块端点：账户、持仓、交易、复盘、绩效、调优、回测。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.auth.dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["trading"])

_store: WatchlistStore | None = None


def set_store(store: WatchlistStore) -> None:
    global _store
    _store = store


def _get_store() -> WatchlistStore:
    if _store is None:
        raise HTTPException(status_code=500, detail="WatchlistStore not initialized")
    return _store


def _user_store(user: dict) -> WatchlistStore:
    return _get_store().for_user(user["sub"])


def _user_budget(user: dict):
    from bottleneck_hunter.watchlist.budget import BudgetTracker
    return BudgetTracker(_user_store(user))


# ─────────────────────────────────────────────────────────
# 模拟账户
# ─────────────────────────────────────────────────────────

@router.get("/account")
async def get_account(market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    return {"account": account, "positions": positions}


@router.get("/account/equity-history")
async def get_equity_history(days: int = 30, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
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


# ─────────────────────────────────────────────────────────
# 持仓
# ─────────────────────────────────────────────────────────

@router.get("/positions")
async def get_positions(include_zero: bool = True, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"), include_zero=include_zero)
    return {"positions": positions}


@router.delete("/positions/{position_id}")
async def delete_position(position_id: str, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    ok = store.delete_sim_position_if_zero(position_id)
    if not ok:
        raise HTTPException(status_code=400, detail="只能删除已清仓（shares=0）的记录")
    return {"status": "deleted"}


@router.get("/positions/{ticker}/trades")
async def get_position_trades(ticker: str, limit: int = 50, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    trades = store.get_sim_trades(ticker=ticker, limit=limit)
    return {"trades": trades}


# ─────────────────────────────────────────────────────────
# 交易历史
# ─────────────────────────────────────────────────────────

@router.get("/trades")
async def get_trades(ticker: str | None = None, side: str | None = None,
                     limit: int = 50, offset: int = 0,
                     market: str = "us_stock",
                     user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    trades = store.get_sim_trades(ticker=ticker, limit=limit)
    if side:
        trades = [t for t in trades if t.get("side") == side]
    return {"trades": trades}


# ─────────────────────────────────────────────────────────
# 资金操作
# ─────────────────────────────────────────────────────────

class AdjustFundsRequest(BaseModel):
    type: str
    amount: float
    note: str = ""


@router.post("/account/adjust-funds")
async def adjust_funds(req: AdjustFundsRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user)
    if req.type not in ("deposit", "withdraw"):
        raise HTTPException(status_code=400, detail="type 必须是 deposit 或 withdraw")
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="金额必须大于 0")
    result = store.adjust_sim_funds(req.type, req.amount, req.note)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/account/fund-ops")
async def get_fund_ops(limit: int = 20, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
    return {"ops": store.get_fund_ops(limit=limit)}


@router.post("/account/refresh-prices")
async def refresh_prices(user: dict = Depends(get_current_user)):
    store = _user_store(user)
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))
    if not positions:
        return {"status": "no_positions"}

    tickers = [p["ticker"] for p in positions if p.get("shares", 0) > 0]
    if not tickers:
        return {"status": "no_active_positions"}

    updated = 0
    try:
        import yfinance as yf
        data = yf.download(tickers, period="1d", progress=False)
        if data.empty:
            return {"status": "no_data"}
        close = data.get("Close")
        if close is None:
            return {"status": "no_close_data"}
        for p in positions:
            if p.get("shares", 0) <= 0:
                continue
            ticker = p["ticker"]
            try:
                price = float(close[ticker].iloc[-1]) if len(tickers) > 1 else float(close.iloc[-1])
                if price > 0:
                    mv = round(p["shares"] * price, 2)
                    pnl = round(p["shares"] * (price - p["avg_cost"]), 2)
                    store.update_sim_position(p["id"], current_price=price,
                                              market_value=mv, unrealized_pnl=pnl)
                    updated += 1
            except Exception:
                continue
    except ImportError:
        return {"status": "yfinance_not_installed"}

    from bottleneck_hunter.watchlist.trade_executor import _recalc_account
    _recalc_account(store, account["id"])

    return {"status": "ok", "updated": updated}


# ─────────────────────────────────────────────────────────
# 交易复盘 & 经验卡片
# ─────────────────────────────────────────────────────────

@router.get("/reviews")
async def get_reviews(ticker: str | None = None, limit: int = 20,
                      user: dict = Depends(get_current_user)):
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
async def run_batch_review_endpoint(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
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
async def get_experience_cards(scope: str | None = None, scope_key: str | None = None,
                               limit: int = 20, user: dict = Depends(get_current_user)):
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
async def get_tuning(status: str | None = None, limit: int = 20,
                     user: dict = Depends(get_current_user)):
    store = _user_store(user)
    return {"proposals": store.get_tuning_proposals(status=status, limit=limit)}


@router.post("/tuning/generate")
async def generate_tuning(request: Request, market: str = "us_stock", user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(market)
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


# ─────────────────────────────────────────────────────────
# 回测
# ─────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None
    benchmark: str = ""
    market: str = "us_stock"


@router.post("/backtest/run")
async def run_backtest(req: BacktestRequest, user: dict = Depends(get_current_user)):
    store = _user_store(user).for_market(req.market)
    benchmark = req.benchmark or ("000300.SS" if req.market == "a_stock" else "SPY")
    from bottleneck_hunter.watchlist.backtest import BacktestEngine
    engine = BacktestEngine(store)
    result = engine.run(req.start_date, req.end_date, benchmark)
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
