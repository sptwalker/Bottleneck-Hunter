"""交易执行引擎 — 确认执行计划后的模拟交易闭环

流程: confirm → create_sim_trade → create/update sim_position → update sim_account
佣金: 0.1% 模拟
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bottleneck_hunter.watchlist.slippage import calc_slippage
from bottleneck_hunter.watchlist.snapshot_binding import bind_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.001


def sane_reprice(new_price: float, ref_price: float, *, lo: float = 0.5, hi: float = 2.0) -> bool:
    """刷价离群守卫：一次刷价内新价相对参考价(上次现价/快照)跌破半价或翻倍，判为外部源脏数据，拒写。

    根治「yfinance/外部源偶发坏 tick 直接落库」——历史上 MU 被写成 10.0(参考价 933，90× 偏离)
    仅靠 `price>0` 放行。ref<=0 时无参考、放行；单票日内真涨跌不会翻倍/腰斩，拒一轮只是留旧值待下轮。
    """
    if not new_price or new_price <= 0:
        return False
    if not ref_price or ref_price <= 0:
        return True
    return lo <= new_price / ref_price <= hi


def _record_exec_failure(store: WatchlistStore, plan_id: str, reason: str) -> None:
    """P0-B（N-2）：成交失败落三处痕迹 —— 计划行计数、operation_log、IM 推送（category=error 在白名单）。

    此前失败只是把状态滚回 pending 就结束了：行看起来跟「刚生成还没轮到」一模一样，
    operation_log 一个字都没有，容器一重启连日志都没了。于是「同一笔单失败 30 次」
    和「今天刚建还没跑」在事后完全无法区分，pending 无限堆积也无人告警。
    """
    try:
        store.record_execution_failure(plan_id, reason)
    except Exception as e:  # noqa: BLE001 —— 计数失败不得影响成交主流程
        logger.debug("失败计数写入失败 plan_id=%s: %s", plan_id, e)
    try:
        uid = getattr(store, "_user_id", "") or ""
        if not uid:
            return
        from bottleneck_hunter.web.oplog import record_operation

        market = getattr(store, "_market", "") or ""
        record_operation(
            uid, "执行计划成交失败",
            category="error",
            detail=f"{market}：计划 {plan_id} 成交失败 —— {reason}"[:300],
            result="fail", market=market,
            meta={"plan_id": plan_id, "reason": reason},
        )
    except Exception as e:  # noqa: BLE001 —— 留痕失败不得影响成交主流程
        logger.debug("成交失败留痕失败 plan_id=%s: %s", plan_id, e)


def _demo() -> None:
    # 坏 tick：933→10 必须拦下；正常波动放行；无参考放行；非正价拒绝
    assert sane_reprice(10.0, 933.44) is False
    assert sane_reprice(958.73, 933.44) is True
    assert sane_reprice(500.0, 0) is True          # 无参考
    assert sane_reprice(0, 933.44) is False         # 非正价
    assert sane_reprice(1900.0, 933.44) is False    # >2x 翻倍
    print("sane_reprice self-check OK")


async def refresh_positions_live(store: WatchlistStore) -> None:
    """确认成交后：拉当前所有持仓的实时价 → 更新持仓市值/浮盈 → 重算账户权益。

    实现"用户批准操作后实时更新持仓"。按持仓所属市场分组刷价；失败静默降级（不阻断确认返回）。
    """
    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch
    account = store.get_sim_account()
    positions = [p for p in store.get_sim_positions(account["id"]) if p.get("shares", 0) > 0]
    if not positions:
        return
    by_market: dict[str, list[str]] = {}
    for p in positions:
        by_market.setdefault(p.get("market") or "us_stock", []).append(p["ticker"])
    for market, tickers in by_market.items():
        mstore = store.for_market(market)
        try:
            await fetch_price_batch(tickers, mstore, market=market)
        except Exception as e:
            logger.warning("确认后拉实时价失败 (%s): %s", market, e)
            continue
        # 用新快照价刷新该市场持仓的市值/浮盈
        for p in mstore.get_sim_positions(account["id"]):
            if p.get("shares", 0) <= 0:
                continue
            snap = mstore.get_latest_snapshot(p["ticker"])
            px = snap.get("close") if snap and snap.get("close") else None
            if not px:
                continue
            if not sane_reprice(px, p.get("current_price") or 0):
                logger.warning("刷价离群拒写 %s: 新价 %s vs 现价 %s", p["ticker"], px, p.get("current_price"))
                continue
            mstore.update_sim_position(
                p["id"], current_price=px,
                market_value=round(p["shares"] * px, 2),
                unrealized_pnl=round(p["shares"] * (px - p["avg_cost"]), 2),
            )
    _recalc_account(store, account["id"])


def execute_trade(store: WatchlistStore, plan_id: str) -> dict:
    """确认执行后的完整交易流程"""
    plan = store.get_execution_plan(plan_id)
    if not plan:
        raise ValueError(f"执行计划 {plan_id} 不存在")

    result_json = plan.get("result_json", {}) if isinstance(plan.get("result_json"), dict) else {}
    market = plan.get("market") or result_json.get("market", "us_stock")
    store = store.for_market(market)
    # 继承决策依据，不代表成交时拉取的行情；领单和建账前拒绝非法绑定。
    try:
        snapshot_id, strategy_version = bind_snapshot(
            snapshot_id=plan.get("snapshot_id"), strategy_version=plan.get("strategy_version"),
            get_snapshot=getattr(store, "get_research_snapshot", None), strict=True,
        )
    except ValueError as exc:
        legacy = plan.get("snapshot_id") is None and plan.get("strategy_version") is None
        message = "历史计划缺少研究快照绑定，请重新生成执行计划" if legacy else f"研究快照绑定无效：{exc}"
        if legacy:
            if plan.get("resting_until"):
                store.expire_execution(plan_id, message)
            else:
                store.reject_execution(plan_id, message)
        logger.warning("拒绝成交 plan_id=%s: %s", plan_id, message)
        _record_exec_failure(store, plan_id, message)
        return {"error": message, "code": "legacy_unbound" if legacy else "invalid_snapshot_binding",
                "plan_id": plan_id}

    account = store.get_sim_account()

    from bottleneck_hunter.watchlist.constraint_validator import validate_execution_plan
    positions = store.get_sim_positions(account["id"])
    is_resting = bool(plan.get("resting_until"))
    # P0-D（N-22）：成交期沿用与生成期**同一判据**——传当刻真实已成交额，
    # 否则会出现「生成期放过、成交期按单笔放行」的错位，日额度形同虚设。
    # 读失败仍放行（宁可漏拦也不误拦真实成交——成交是用户已经决定的事），但**必须留痕**：
    # 静默降级会让日换手卡口在无人知情的情况下失效，出了事连"当时闸门是不是坏的"都查不出来。
    # 只告警不累加本计划的失败计数——读库抖动不是这笔单的错，计进去会被老化 reaper 误清。
    try:
        _turnover_used = store.daily_turnover_amount(account.get("id", ""), market)
    except Exception as e:  # noqa: BLE001 —— 取不到按 0（放行），不误拦真实成交
        logger.warning("当日已成交额读取失败，日换手卡口本轮放行 plan_id=%s: %s", plan_id, e)
        try:
            uid = getattr(store, "_user_id", "") or ""
            if uid:
                from bottleneck_hunter.web.oplog import record_operation

                record_operation(
                    uid, "日换手卡口降级",
                    category="error",  # 闸门失效要被看见，同「质量门阻断」进推送白名单
                    detail=f"{market}：当日已成交额读取失败，本轮按 0 放行（日换手上限暂不生效）：{e}"[:300],
                    result="partial", market=market,
                    meta={"plan_id": plan_id, "error": str(e)},
                )
        except Exception as le:  # noqa: BLE001 —— 留痕失败不得影响成交
            logger.debug("日换手降级留痕失败 plan_id=%s: %s", plan_id, le)
        _turnover_used = 0.0
    validation = validate_execution_plan(plan, account, positions, daily_turnover_used=_turnover_used)
    if not validation.valid:
        if is_resting:
            # 挂单轮询时约束暂不满足（如现金临时不足/占比超限）→ 保持挂单，绝不撤单。
            return {"error": "约束暂不满足，保持挂单", "violations": validation.violations,
                    "resting_hold": True, "plan_id": plan_id}
        store.reject_execution(plan_id, "; ".join(validation.violations))
        _record_exec_failure(store, plan_id, "约束校验不通过：" + "; ".join(validation.violations))
        return {"error": "约束校验不通过", "violations": validation.violations, "plan_id": plan_id}

    action = plan.get("action") or result_json.get("action", "")
    from bottleneck_hunter.watchlist.store_base import normalize_ticker
    # 归一：执行计划 .SH 与观察池/持仓 .SS 对齐，杜绝重复持仓/误报持仓不足
    ticker = normalize_ticker(plan.get("ticker", ""), market)
    shares = plan.get("shares") or result_json.get("shares", 0)
    planned_price = (plan.get("target_price")
                     or result_json.get("target_price")
                     or result_json.get("estimated_price", 0))
    try:
        planned_price = float(planned_price) if planned_price not in (None, "") else 0.0
    except (TypeError, ValueError):
        planned_price = 0.0  # 挂单价脏数据(字符串/None)→按市价单处理，不再拿它跟市价比大小

    # 诚信/防前视偏差：成交必须基于真实市价快照。取不到真实快照时【拒绝成交】，
    # 绝不用 L4 规划时 LLM 估的目标价成交（那会用幻觉价污染 sim_trades / win_rate / 自进化回路）。
    snap = store.get_latest_snapshot(ticker)
    real_px = snap.get("close") if snap and snap.get("close") else None
    if not real_px:
        _record_exec_failure(store, plan_id, f"{ticker} 无真实市价快照，拒绝以 LLM 估价成交")
        return {"error": "无真实市价快照，拒绝以 LLM 估价成交", "plan_id": plan_id,
                "needs": "price_snapshot", "ticker": ticker}
    # 挂单自动撮合：行情快照过旧(>36h)时不自动成交，等下次刷价再判。手动确认(非挂单)不受此限——用户在场。
    if is_resting:
        fetched = snap.get("fetched_at") or ""
        stale_before = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat(timespec="seconds")
        if fetched and fetched < stale_before:
            return {"error": "行情快照过旧，暂缓自动撮合", "stale": True, "plan_id": plan_id, "ticker": ticker}
    exec_basis = real_px

    if not action or not ticker or not shares or not exec_basis:
        _record_exec_failure(store, plan_id, "执行计划缺少关键字段（action/ticker/shares/price）")
        return {"error": "执行计划缺少关键字段", "plan_id": plan_id}

    # 限价单：仅对买卖动作生效、挂单价须为正。买/加仓 市价≤挂单价、卖/减仓 市价≥挂单价 才成交；
    # 未达则自动转「挂单」等待（不用 LLM 估价成交），最长 2 周由轮询任务续判/到期。
    # 无挂单价 = 市价单立即成交。成交价永远用真实市价快照（对用户有利一侧）。取代原「偏离>30% 拒绝」。
    if action in ("buy", "add", "sell", "reduce") and planned_price and planned_price > 0:
        favorable = (real_px <= planned_price) if action in ("buy", "add") else (real_px >= planned_price)
        if not favorable:
            until = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(timespec="seconds")
            store.rest_execution(plan_id, until)
            return {"rested": True, "plan_id": plan_id, "ticker": ticker, "action": action,
                    "limit_price": planned_price, "market_price": real_px}

    # 原子领单：confirmed→executed CAS 抢占，杜绝与并发取消/轮询的竞态孤儿成交。
    # 领单失败 = 已被取消/成交，直接跳过；下方成交失败再回滚领单，恢复原状态（含挂单）。
    prev_resting_until = plan.get("resting_until") or ""
    if not store.mark_executed(plan_id):
        return {"error": "计划状态已变更（已取消/已成交），跳过成交", "stale": True, "plan_id": plan_id}

    if action in ("buy", "add"):
        result = _execute_buy(store, account, plan_id, ticker, shares, exec_basis,
                              plan.get("entry_id"), result_json.get("reasoning", ""),
                              market=market, snapshot_id=snapshot_id, strategy_version=strategy_version)
    elif action in ("sell", "reduce"):
        result = _execute_sell(store, account, plan_id, ticker, shares, exec_basis,
                               plan.get("entry_id"), result_json.get("reasoning", ""),
                               market=market, snapshot_id=snapshot_id, strategy_version=strategy_version)
    else:
        store.unclaim_execution(plan_id, prev_resting_until)
        _record_exec_failure(store, plan_id, f"不支持的操作类型: {action}")
        return {"error": f"不支持的操作类型: {action}", "plan_id": plan_id}

    if "error" in result:
        # 成交失败 → 回滚领单，恢复 confirmed(含挂单)，可重试/继续挂单
        store.unclaim_execution(plan_id, prev_resting_until)
        _record_exec_failure(store, plan_id, str(result.get("error", "")))
        return result

    _recalc_account(store, account["id"])
    if action in ("sell", "reduce"):
        trade_id = result.get("trade_id", "")
        is_win = result.get("realized_pnl", 0) > 0
        _update_card_outcomes(store, plan_id, is_win)
        if trade_id:
            _schedule_auto_review(store, trade_id)

    return result


async def confirm_and_execute(store: WatchlistStore, plan_id: str) -> dict:
    """确认单条执行计划并成交 —— 确认端点与 L4 自动执行共用同一条闭环。

    抽取自原 decision_api 确认端点：confirm → execute_trade →（缺快照按需拉价重试一次）
    →（未达限价转挂单 / 业务错误回滚 pending）→ 成交后实时刷持仓。
    返回 {"status": ...}：not_found / exception / resting / error / confirmed。
    调用方（HTTP 端点）据 status 决定是否抛 404/500；自动执行则只据 status 计数。
    """
    ok = store.confirm_execution(plan_id)
    if not ok:
        return {"status": "not_found"}
    try:
        trade_result = execute_trade(store, plan_id)
        # 缺真实市价快照 → 按需拉一次价再重试一次（观察池刷新可能漏了该票/该周期失败）。
        if trade_result.get("needs") == "price_snapshot":
            tk = trade_result.get("ticker")
            plan = store.get_execution_plan(plan_id) or {}
            mkt = plan.get("market") or (plan.get("result_json") or {}).get("market", "us_stock")
            if tk:
                try:
                    from bottleneck_hunter.watchlist.price_pipeline import fetch_price_batch
                    await fetch_price_batch([tk], store.for_market(mkt), market=mkt)
                    trade_result = execute_trade(store, plan_id)  # 单次重试，避免死循环
                except Exception:
                    logger.warning("按需拉价重试失败 plan_id=%s ticker=%s", plan_id, tk)
    except Exception as e:
        logger.exception("交易执行异常 plan_id=%s", plan_id)
        # 执行失败回滚到 pending，避免卡在 confirmed
        try:
            store.revert_to_pending(plan_id)
        except Exception:
            logger.warning("回滚 plan_id=%s 到 pending 失败", plan_id)
        _record_exec_failure(store, plan_id, f"交易执行异常：{e}")
        return {"status": "exception", "error": str(e)}
    # 未达限价 → 已自动转挂单（不算失败，不回滚 pending）
    if trade_result.get("rested"):
        return {"status": "resting", "trade": trade_result,
                "limit_price": trade_result.get("limit_price"),
                "market_price": trade_result.get("market_price")}
    # execute_trade 返回 error 字段表示业务错误（约束不通过、现金不足、缺价等）
    if "error" in trade_result:
        try:
            store.revert_to_pending(plan_id)
        except Exception:
            logger.warning("业务错误回滚 plan_id=%s 到 pending 失败", plan_id)
        return {"status": "error", "trade": trade_result, "message": trade_result["error"]}
    # 成交成功 → 拉实时价重算持仓。失败静默降级不阻断返回。
    try:
        await refresh_positions_live(store)
    except Exception:
        logger.warning("确认后实时刷新持仓失败 plan_id=%s", plan_id)
    return {"status": "confirmed", "trade": trade_result}


def _schedule_auto_review(store: WatchlistStore, trade_id: str) -> None:
    """触发卖出后自动复盘。诚信原则：不能像旧版那样"无事件循环就静默跳过"
    （那正是 auto_reviews 表长期为空的根因）。有运行中的 loop → 后台任务；
    否则（同步上下文/脚本/定时任务）→ 同步跑完，保证复盘一定发生。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_auto_review_sell(store, trade_id))
    except RuntimeError:
        # 无运行中的事件循环：同步执行到底，绝不跳过
        try:
            asyncio.run(_auto_review_sell(store, trade_id))
        except Exception as e:
            logger.error("同步自动复盘失败 %s: %s", trade_id, e)


def _execute_buy(store: WatchlistStore, account: dict,
                 plan_id: str, ticker: str, shares: int, price: float,
                 entry_id: str | None, reasoning: str,
                 market: str = "us_stock", snapshot_id: str | None = None,
                 strategy_version: str | None = None) -> dict:
    avg_vol = _get_avg_volume(store, ticker)
    exec_price, slippage_bps = calc_slippage(price, shares, "buy", market, avg_vol)

    amount = round(shares * exec_price, 2)
    commission = round(amount * COMMISSION_RATE, 2)
    total_cost = amount + commission

    if account.get("cash_balance", 0) < total_cost:
        return {"error": "现金不足", "required": total_cost,
                "available": account.get("cash_balance", 0)}

    trade_id = store.create_sim_trade(
        account_id=account["id"], ticker=ticker, side="buy",
        shares=shares, price=exec_price, amount=amount,
        execution_plan_id=plan_id, entry_id=entry_id,
        trade_type="entry", reasoning=reasoning,
        slippage_bps=slippage_bps,
        snapshot_id=snapshot_id, strategy_version=strategy_version, strict=True,
    )

    pos = store.get_sim_position_any(account["id"], ticker)
    if pos:
        old_shares = pos["shares"]
        old_cost = pos["avg_cost"]
        new_shares = old_shares + shares
        new_avg_cost = round((old_shares * old_cost + amount) / new_shares, 4) if new_shares else exec_price
        store.update_sim_position(pos["id"],
                                  shares=new_shares,
                                  avg_cost=new_avg_cost,
                                  current_price=exec_price,
                                  market_value=round(new_shares * exec_price, 2),
                                  unrealized_pnl=round(new_shares * (exec_price - new_avg_cost), 2))
    else:
        store.create_sim_position(
            account_id=account["id"], ticker=ticker,
            shares=shares, avg_cost=exec_price, entry_id=entry_id,
        )

    new_cash = round(account["cash_balance"] - total_cost, 2)
    store.update_sim_account(cash_balance=new_cash)

    return {
        "trade_id": trade_id, "side": "buy", "ticker": ticker,
        "shares": shares, "price": exec_price, "target_price": price,
        "slippage_bps": slippage_bps, "amount": amount,
        "commission": commission, "cash_after": new_cash,
    }


def _execute_sell(store: WatchlistStore, account: dict,
                  plan_id: str, ticker: str, shares: int, price: float,
                  entry_id: str | None, reasoning: str,
                  market: str = "us_stock", snapshot_id: str | None = None,
                 strategy_version: str | None = None) -> dict:
    pos = store.get_sim_position(account["id"], ticker)
    if not pos or pos["shares"] < shares:
        return {"error": "持仓不足", "required": shares,
                "available": pos["shares"] if pos else 0}

    avg_vol = _get_avg_volume(store, ticker)
    exec_price, slippage_bps = calc_slippage(price, shares, "sell", market, avg_vol)

    amount = round(shares * exec_price, 2)
    commission = round(amount * COMMISSION_RATE, 2)
    net_proceeds = amount - commission

    realized_pnl = round((exec_price - pos["avg_cost"]) * shares - commission, 2)

    trade_id = store.create_sim_trade(
        account_id=account["id"], ticker=ticker, side="sell",
        shares=shares, price=exec_price, amount=amount,
        execution_plan_id=plan_id, entry_id=entry_id,
        trade_type="exit", reasoning=reasoning,
        slippage_bps=slippage_bps,
        realized_pnl=realized_pnl,
        snapshot_id=snapshot_id, strategy_version=strategy_version, strict=True,
    )

    # B4: 用实际盈亏结算该 ticker 的投委会投票预测，让委员历史校准权重真正生效
    # （盈利→票判对；亏损→票判错。score_delta 借 record_outcome 的 is_correct 阈值语义：<2 判对）
    try:
        won = realized_pnl > 0
        store.record_outcome(ticker, "vote",
                             outcome_value="win" if won else "loss",
                             score_delta=0.0 if won else 3.0)
    except Exception:
        logger.debug("record_outcome(vote) failed for %s", ticker)

    remaining = pos["shares"] - shares
    if remaining <= 0:
        store.update_sim_position(pos["id"], shares=0, current_price=exec_price,
                                  market_value=0, unrealized_pnl=0, weight_pct=0)
    else:
        store.update_sim_position(pos["id"],
                                  shares=remaining,
                                  current_price=exec_price,
                                  market_value=round(remaining * exec_price, 2),
                                  unrealized_pnl=round(remaining * (exec_price - pos["avg_cost"]), 2))

    new_cash = round(account["cash_balance"] + net_proceeds, 2)
    store.update_sim_account(cash_balance=new_cash)

    return {
        "trade_id": trade_id, "side": "sell", "ticker": ticker,
        "shares": shares, "price": exec_price, "target_price": price,
        "slippage_bps": slippage_bps, "amount": amount,
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

    trades = store.get_sim_trades(limit=10000, account_id=account_id)
    total_trades = len(trades)

    sell_trades = [t for t in trades if t.get("side") == "sell"]
    if sell_trades:
        # 用持久化的 realized_pnl 判定盈亏（比按均价反推更准）；旧数据无该字段时回退估算。
        def _is_win(t):
            rp = t.get("realized_pnl")
            if rp is not None:
                return rp > 0
            return t.get("amount", 0) > t.get("shares", 1) * _find_avg_cost(trades, t["ticker"])
        winning = sum(1 for t in sell_trades if _is_win(t))
        win_rate = round(winning / len(sell_trades) * 100, 2)
    else:
        win_rate = 0.0

    store.update_sim_account(
        total_equity=total_equity,
        current_capital=total_equity,
        total_return_pct=total_return_pct,
        total_trades=total_trades,
        win_rate=win_rate,
        peak_equity=max(account.get("peak_equity", 0) or 0, total_equity),
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


async def _auto_review_sell(store: WatchlistStore, trade_id: str) -> None:
    """卖出后自动触发 LLM 复盘（后台任务，不阻塞执行响应）。"""
    try:
        from bottleneck_hunter.watchlist.budget import BudgetTracker
        budget = BudgetTracker(store)
        if not budget.can_spend():
            logger.info("预算不足，跳过自动复盘 %s", trade_id)
            return
        from bottleneck_hunter.watchlist.trade_reviewer import run_trade_review
        async for evt in run_trade_review(store, trade_id, budget):
            data = evt.get("data", {})
            if isinstance(data, dict) and "error" in data.get("event", ""):
                logger.warning("自动复盘失败 %s: %s", trade_id, data)
        logger.info("自动复盘完成 %s", trade_id)
    except Exception as e:
        logger.error("自动复盘异常 %s: %s", trade_id, e)


def _get_avg_volume(store: WatchlistStore, ticker: str) -> int | None:
    """获取近期日均成交量，供滑点计算使用"""
    try:
        snapshots = store.get_snapshots(ticker, days=20)
        volumes = [s.get("volume", 0) for s in snapshots if s.get("volume")]
        return int(sum(volumes) / len(volumes)) if volumes else None
    except Exception:
        return None


def _update_card_outcomes(store: WatchlistStore, plan_id: str, is_win: bool) -> None:
    """卖出后根据盈亏更新关联经验卡片的置信度"""
    try:
        plan = store.get_execution_plan(plan_id)
        if not plan:
            return
        result_json = plan.get("result_json", {})
        if isinstance(result_json, str):
            import json
            try:
                result_json = json.loads(result_json)
            except (json.JSONDecodeError, TypeError):
                return
        card_ids = result_json.get("applied_card_ids", [])
        for cid in card_ids:
            store.update_card_outcome(cid, is_win)
            logger.info("经验卡片 %s 结果更新: %s", cid, "盈利" if is_win else "亏损")
    except Exception as e:
        logger.warning("更新经验卡片结果失败: %s", e)
