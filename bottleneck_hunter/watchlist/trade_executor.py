"""交易执行引擎 — 确认执行计划后的模拟交易闭环

流程: confirm → create_sim_trade → create/update sim_position → update sim_account
佣金: 0.1% 模拟
"""

from __future__ import annotations

import logging
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.001


def execute_trade(store: WatchlistStore, plan_id: str) -> dict:
    """确认执行后的完整交易流程"""
    plan = store.get_execution_plan(plan_id)
    if not plan:
        raise ValueError(f"执行计划 {plan_id} 不存在")

    account = store.get_sim_account()
    result_json = plan.get("result_json", {}) if isinstance(plan.get("result_json"), dict) else {}

    action = plan.get("action") or result_json.get("action", "")
    ticker = plan.get("ticker", "")
    shares = plan.get("shares") or result_json.get("shares", 0)
    target_price = (plan.get("target_price")
                    or result_json.get("target_price")
                    or result_json.get("estimated_price", 0))

    if not action or not ticker or not shares or not target_price:
        return {"error": "执行计划缺少关键字段", "plan_id": plan_id}

    if action in ("buy", "add"):
        result = _execute_buy(store, account, plan_id, ticker, shares, target_price,
                              plan.get("entry_id"), result_json.get("reasoning", ""))
    elif action in ("sell", "reduce"):
        result = _execute_sell(store, account, plan_id, ticker, shares, target_price,
                               plan.get("entry_id"), result_json.get("reasoning", ""))
    else:
        return {"error": f"不支持的操作类型: {action}", "plan_id": plan_id}

    if "error" not in result:
        _recalc_account(store, account["id"])

    return result


def _execute_buy(store: WatchlistStore, account: dict,
                 plan_id: str, ticker: str, shares: int, price: float,
                 entry_id: str | None, reasoning: str) -> dict:
    amount = shares * price
    commission = round(amount * COMMISSION_RATE, 2)
    total_cost = amount + commission

    if account.get("cash_balance", 0) < total_cost:
        return {"error": "现金不足", "required": total_cost,
                "available": account.get("cash_balance", 0)}

    trade_id = store.create_sim_trade(
        account_id=account["id"], ticker=ticker, side="buy",
        shares=shares, price=price, amount=amount,
        execution_plan_id=plan_id, entry_id=entry_id,
        trade_type="entry", reasoning=reasoning,
    )

    pos = store.get_sim_position(account["id"], ticker)
    if pos:
        old_shares = pos["shares"]
        old_cost = pos["avg_cost"]
        new_shares = old_shares + shares
        new_avg_cost = round((old_shares * old_cost + amount) / new_shares, 4)
        store.update_sim_position(pos["id"],
                                  shares=new_shares,
                                  avg_cost=new_avg_cost,
                                  current_price=price,
                                  market_value=round(new_shares * price, 2),
                                  unrealized_pnl=round(new_shares * (price - new_avg_cost), 2))
    else:
        store.create_sim_position(
            account_id=account["id"], ticker=ticker,
            shares=shares, avg_cost=price, entry_id=entry_id,
        )

    new_cash = round(account["cash_balance"] - total_cost, 2)
    store.update_sim_account(cash_balance=new_cash)

    return {
        "trade_id": trade_id, "side": "buy", "ticker": ticker,
        "shares": shares, "price": price, "amount": amount,
        "commission": commission, "cash_after": new_cash,
    }


def _execute_sell(store: WatchlistStore, account: dict,
                  plan_id: str, ticker: str, shares: int, price: float,
                  entry_id: str | None, reasoning: str) -> dict:
    pos = store.get_sim_position(account["id"], ticker)
    if not pos or pos["shares"] < shares:
        return {"error": "持仓不足", "required": shares,
                "available": pos["shares"] if pos else 0}

    amount = shares * price
    commission = round(amount * COMMISSION_RATE, 2)
    net_proceeds = amount - commission

    trade_id = store.create_sim_trade(
        account_id=account["id"], ticker=ticker, side="sell",
        shares=shares, price=price, amount=amount,
        execution_plan_id=plan_id, entry_id=entry_id,
        trade_type="exit", reasoning=reasoning,
    )

    realized_pnl = round((price - pos["avg_cost"]) * shares - commission, 2)

    remaining = pos["shares"] - shares
    if remaining <= 0:
        store.delete_sim_position(pos["id"])
    else:
        store.update_sim_position(pos["id"],
                                  shares=remaining,
                                  current_price=price,
                                  market_value=round(remaining * price, 2),
                                  unrealized_pnl=round(remaining * (price - pos["avg_cost"]), 2))

    new_cash = round(account["cash_balance"] + net_proceeds, 2)
    store.update_sim_account(cash_balance=new_cash)

    return {
        "trade_id": trade_id, "side": "sell", "ticker": ticker,
        "shares": shares, "price": price, "amount": amount,
        "commission": commission, "realized_pnl": realized_pnl,
        "cash_after": new_cash,
    }


def _recalc_account(store: WatchlistStore, account_id: str) -> None:
    """重新计算账户总权益、收益率、胜率"""
    account = store.get_sim_account()
    positions = store.get_sim_positions(account_id)

    position_value = sum(p.get("market_value", 0) for p in positions)
    total_equity = round(account["cash_balance"] + position_value, 2)
    initial = account.get("initial_capital", 100000)
    total_return_pct = round((total_equity / initial - 1) * 100, 2) if initial else 0.0

    trades = store.get_sim_trades(limit=10000)
    total_trades = len(trades)

    sell_trades = [t for t in trades if t.get("side") == "sell"]
    if sell_trades:
        winning = sum(1 for t in sell_trades
                      if t.get("amount", 0) > t.get("shares", 1) * _find_avg_cost(trades, t["ticker"]))
        win_rate = round(winning / len(sell_trades) * 100, 2)
    else:
        win_rate = 0.0

    store.update_sim_account(
        total_equity=total_equity,
        current_capital=total_equity,
        total_return_pct=total_return_pct,
        total_trades=total_trades,
        win_rate=win_rate,
    )

    if positions and total_equity > 0:
        for p in positions:
            weight = round(p.get("market_value", 0) / total_equity * 100, 2)
            store.update_sim_position(p["id"], weight_pct=weight)


def _find_avg_cost(trades: list[dict], ticker: str) -> float:
    """从交易记录中估算某 ticker 的买入均价"""
    buys = [t for t in trades if t.get("ticker") == ticker and t.get("side") == "buy"]
    if not buys:
        return 0.0
    total_shares = sum(t.get("shares", 0) for t in buys)
    total_amount = sum(t.get("amount", 0) for t in buys)
    return total_amount / total_shares if total_shares else 0.0
