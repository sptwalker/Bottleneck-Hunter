"""交易复盘引擎 — 卖出交易后的 LLM 归因分析

对已完成的卖出交易进行自动复盘：对比入场逻辑与实际结果，
提取经验教训，生成经验卡片供后续决策参考。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.llm_clients.factory import get_llm_for_position
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.chain.json_utils import extract_json_object

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": {"event": event, **data}}



def _rule_attribution(
    return_pct: float, entry_price: float, exit_price: float,
    period_high: float, period_low: float, benchmark_return_pct: float,
) -> dict:
    """基于规则的简易归因分析"""
    alpha = round(return_pct - benchmark_return_pct, 2)
    stock_score = 7 if return_pct > 5 else 5 if return_pct >= 0 else 3

    timing_score = 5
    capture_ratio = 0
    if period_high and entry_price:
        max_possible = (period_high / entry_price - 1) * 100
        capture_ratio = return_pct / max_possible if max_possible > 0 else 0
        if capture_ratio > 0.7:
            timing_score = 8
        elif capture_ratio > 0.4:
            timing_score = 6
        else:
            timing_score = 3

    macro_score = 7 if alpha > 2 else 5 if alpha >= -2 else 3

    timing_assessment = f"捕获最大潜在收益的 {capture_ratio * 100:.0f}%" if (period_high and entry_price) else "无数据"
    if period_high:
        timing_assessment += f"（期间最高 {period_high:.2f}）"

    return {
        "stock_selection": {
            "score": stock_score,
            "assessment": f"择股{'正确' if return_pct > 0 else '待验证'}，收益 {return_pct:+.1f}%"
        },
        "market_timing": {
            "score": timing_score,
            "assessment": timing_assessment,
        },
        "macro_alignment": {
            "score": macro_score,
            "assessment": f"Alpha {alpha:+.1f}%（基准 {benchmark_return_pct:+.1f}%）"
        },
        "plan_deviation": {
            "entry_diff_pct": 0,
            "exit_diff_pct": 0,
            "assessment": "规则复盘无计划目标价对比"
        },
    }


def _rule_based_review(
    ticker: str, entry_price: float, exit_price: float,
    return_pct: float, holding_days: int,
    period_high: float = 0, period_low: float = 0,
    benchmark_return_pct: float = 0,
) -> dict:
    """基于规则的简易复盘 — 当 LLM 不可用或调用失败时的 fallback。"""
    # 交易质量评分：按收益率和持仓合理性打分（1-10）
    if return_pct >= 20:
        quality = 8
        outcome = "优秀"
    elif return_pct >= 5:
        quality = 7
        outcome = "良好"
    elif return_pct >= 0:
        quality = 5
        outcome = "保本"
    elif return_pct >= -5:
        quality = 4
        outcome = "小亏"
    elif return_pct >= -15:
        quality = 3
        outcome = "中等亏损"
    else:
        quality = 2
        outcome = "大幅亏损"

    # 持仓天数评价
    if holding_days <= 3:
        timing_note = "持仓过短，可能属于频繁交易"
    elif holding_days <= 30:
        timing_note = "短线持仓，注意交易频率"
    elif holding_days <= 90:
        timing_note = "中线持仓，持仓周期合理"
    else:
        timing_note = "长线持仓，检查是否错过止盈/止损时机"

    lessons = []
    if return_pct < -10:
        lessons.append("止损纪律需加强，亏损超过 10%")
    if return_pct > 0 and holding_days < 5:
        lessons.append("盈利但持仓太短，可能错过更大利润空间")
    if return_pct < 0 and holding_days > 60:
        lessons.append("长期持有但亏损，需检查入场逻辑是否合理")
    if not lessons:
        lessons.append(f"交易结果{outcome}，继续保持当前策略纪律")

    exp_card = None
    # 只对显著盈亏生成经验卡片
    if abs(return_pct) >= 10:
        if return_pct >= 10:
            exp_card = {
                "scope": "ticker", "scope_key": ticker,
                "category": "pattern",
                "title": f"{ticker} 盈利 {return_pct:+.1f}% 模式（规则复盘）",
                "content": f"入场价 {entry_price:.2f}，出场价 {exit_price:.2f}，"
                           f"持仓 {holding_days} 天。{timing_note}",
                "confidence": 0.4,
            }
        else:
            exp_card = {
                "scope": "ticker", "scope_key": ticker,
                "category": "lesson",
                "title": f"{ticker} 亏损 {return_pct:+.1f}% 教训（规则复盘）",
                "content": f"入场价 {entry_price:.2f}，出场价 {exit_price:.2f}，"
                           f"持仓 {holding_days} 天。{'; '.join(lessons)}",
                "confidence": 0.4,
            }

    return {
        "trade_quality_score": quality,
        "outcome_summary": f"{ticker} {outcome}：收益 {return_pct:+.1f}%，持仓 {holding_days} 天",
        "key_lessons": lessons,
        "timing_analysis": timing_note,
        "review_method": "rule_based_fallback",
        "attribution": _rule_attribution(
            return_pct, entry_price, exit_price,
            period_high, period_low, benchmark_return_pct,
        ),
        "experience_card": exp_card or {},
    }


async def run_trade_review(
    store: WatchlistStore,
    trade_id: str,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """对单笔卖出交易进行 LLM 复盘"""
    sell_trade = None
    trades = store.get_sim_trades(limit=10000)
    for t in trades:
        if t["id"] == trade_id:
            sell_trade = t
            break

    if not sell_trade or sell_trade.get("side") != "sell":
        yield _sse("review_error", trade_id=trade_id, error="找不到卖出交易")
        return

    ticker = sell_trade["ticker"]
    exit_price = sell_trade["price"]

    yield _sse("review_start", ticker=ticker, trade_id=trade_id,
               message=f"开始复盘 {ticker} 卖出交易...")

    buy_trades = [t for t in trades if t["ticker"] == ticker and t["side"] == "buy"]
    if buy_trades:
        entry_price = buy_trades[0].get("price", 0)
        buy_date = buy_trades[0].get("created_at", "")
    else:
        entry_price = 0
        buy_date = ""

    return_pct = round((exit_price / entry_price - 1) * 100, 2) if entry_price else 0

    holding_days = 0
    if buy_date:
        try:
            buy_dt = datetime.fromisoformat(buy_date.replace("Z", "+00:00"))
            sell_dt = datetime.fromisoformat(
                sell_trade.get("created_at", "").replace("Z", "+00:00"))
            holding_days = (sell_dt - buy_dt).days
        except (ValueError, TypeError):
            pass

    exec_plan_id = sell_trade.get("execution_plan_id", "")
    execution_plan = ""
    committee_review = ""
    if exec_plan_id:
        plan = store.get_execution_plan(exec_plan_id)
        if plan:
            rj = plan.get("result_json", {})
            if isinstance(rj, str):
                try:
                    rj = json.loads(rj)
                except (json.JSONDecodeError, TypeError):
                    rj = {}
            execution_plan = json.dumps(rj, ensure_ascii=False)[:1500]

        reviews = store.get_reviews_for_execution(exec_plan_id)
        if reviews:
            committee_review = json.dumps(
                [{"role": r.get("member_role", ""), "verdict": r.get("verdict", ""),
                  "summary": r.get("summary", "")[:200]} for r in reviews[:4]],
                ensure_ascii=False)

    entry_id = sell_trade.get("entry_id", "")
    catalysts = store.get_catalysts_for_entry(entry_id) if entry_id else []

    # 确定市场类型，用于选择基准指数
    market = "us_stock"
    if entry_id:
        wl_entry = store.get(entry_id)
        if wl_entry:
            market = wl_entry.get("market", "us_stock")
    benchmark_ticker = "000300.SH" if market == "a_stock" else "SPY"
    catalyst_status = json.dumps(
        [{"title": c.get("title", ""), "status": c.get("status", ""),
          "expected_date": c.get("expected_date", "")} for c in catalysts[:5]],
        ensure_ascii=False) if catalysts else "无相关催化剂"

    # ── 19E: 归因分析数据收集 ──
    period_high, period_low, benchmark_return_pct = 0.0, 0.0, 0.0
    period_market_data = "无持仓期间数据"
    if buy_date and entry_price:
        start_date = buy_date[:10]
        end_date = sell_trade.get("created_at", "")[:10]
        try:
            snapshots = store.get_snapshots(ticker, days=500)
            period_snaps = [s for s in snapshots
                           if start_date <= s.get("date", "")[:10] <= end_date]
            if period_snaps:
                highs = [s.get("high", 0) for s in period_snaps if s.get("high")]
                lows = [s.get("low", 0) for s in period_snaps if s.get("low")]
                period_high = max(highs) if highs else exit_price
                period_low = min(lows) if lows else entry_price
                period_market_data = (
                    f"持仓期间 {start_date} ~ {end_date}:\n"
                    f"- 最高价: {period_high:.2f}\n"
                    f"- 最低价: {period_low:.2f}\n"
                    f"- 最大潜在收益: {((period_high / entry_price - 1) * 100):.1f}%\n"
                    f"- 最大潜在回撤: {((period_low / entry_price - 1) * 100):.1f}%"
                )
        except Exception as e:
            logger.debug("获取持仓期间数据失败: %s", e)

        try:
            benchmark_return_pct = store.get_benchmark_return(
                start_date, end_date, benchmark=benchmark_ticker)
        except Exception as e:
            logger.debug("获取基准收益率失败: %s", e)

    # ── 尝试 LLM 复盘，失败则降级为规则复盘 ──
    use_fallback = False
    fallback_reason = ""

    llm, provider, model = get_llm_for_position(position="watchlist_trade_review", temperature=0.2)
    if not llm:
        use_fallback = True
        fallback_reason = "无可用 LLM"
    elif budget and not budget.can_spend(estimated_tokens=2000):
        use_fallback = True
        fallback_reason = "预算不足"

    result = None
    if not use_fallback:
        try:
            prompt_template = (PROMPTS_DIR / "trade_review.md").read_text(encoding="utf-8")
            prompt = (prompt_template
                      .replace("{ticker}", ticker)
                      .replace("{entry_price}", f"{entry_price:.2f}")
                      .replace("{exit_price}", f"{exit_price:.2f}")
                      .replace("{return_pct}", f"{return_pct:.2f}")
                      .replace("{holding_days}", str(holding_days))
                      .replace("{period_market_data}", period_market_data)
                      .replace("{period_high}", f"{period_high:.2f}")
                      .replace("{period_low}", f"{period_low:.2f}")
                      .replace("{benchmark_return_pct}", f"{benchmark_return_pct:.2f}")
                      .replace("{execution_plan}", execution_plan or "无执行计划记录")
                      .replace("{committee_review}", committee_review or "无投委会评审记录")
                      .replace("{catalyst_status}", catalyst_status))

            yield _sse("review_progress", ticker=ticker, message=f"{ticker} LLM 分析中...")

            response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

            if budget:
                budget.record(provider, model, 1500, 800, "trade_review")

            result = extract_json_object(response)

        except Exception as e:
            logger.warning("LLM 复盘失败，降级为规则复盘: %s — %s", ticker, e)
            use_fallback = True
            fallback_reason = f"LLM 调用失败: {e}"

    # ── 规则化 fallback：无需 LLM 的简易复盘 ──
    if use_fallback or result is None:
        logger.info("使用规则化 fallback 复盘 %s（原因: %s）", ticker, fallback_reason)
        yield _sse("review_progress", ticker=ticker,
                   message=f"{ticker} 规则化复盘中（{fallback_reason}）...")
        result = _rule_based_review(
            ticker=ticker, entry_price=entry_price, exit_price=exit_price,
            return_pct=return_pct, holding_days=holding_days,
            period_high=period_high, period_low=period_low,
            benchmark_return_pct=benchmark_return_pct,
        )

    exp_card_data = result.get("experience_card", {})

    review_id = store.create_auto_review(
        sim_trade_id=trade_id,
        ticker=ticker,
        review_type="trade_close",
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=return_pct,
        result_json=result,
        lessons_learned="; ".join(result.get("key_lessons", [])),
        experience_card=exp_card_data,
    )

    # P1.2 分层绩效归因：从复盘 attribution 拆出四层评分
    # 诚信原则：失败要以 error 级别可见（历史上此表长期 0 行且无人知晓）。
    try:
        attribution = result.get("attribution", {})
        if attribution:
            store.record_layer_performance(trade_id, ticker, attribution, return_pct)
            logger.info("分层绩效已记录: trade=%s ticker=%s layers=%s",
                        trade_id, ticker, list(attribution.keys()))
        else:
            logger.warning("复盘结果缺少 attribution，分层绩效未记录: trade=%s ticker=%s", trade_id, ticker)
    except Exception as e:
        logger.error("record_layer_performance failed for %s: %s", ticker, e, exc_info=True)

    if exp_card_data and exp_card_data.get("title"):
        try:
            store.create_experience_card(
                scope=exp_card_data.get("scope", "global"),
                scope_key=exp_card_data.get("scope_key", ""),
                category=exp_card_data.get("category", "lesson"),
                title=exp_card_data["title"],
                content=exp_card_data.get("content", ""),
                evidence=[f"{ticker}: {return_pct:+.1f}% ({holding_days}d)"],
                confidence=exp_card_data.get("confidence", 0.5),
                source_review_id=review_id,
            )
        except Exception as e:
            logger.warning("经验卡片写入失败 %s: %s", ticker, e)

    # 复盘完成后，同步更新投资论点证据日志
    try:
        if entry_id:
            theses = store.get_theses_for_entry(entry_id, active_only=True)
            for thesis in theses:
                direction = "supporting" if return_pct > 0 else "contradicting"
                impact = "strengthens" if return_pct > 5 else "weakens" if return_pct < -5 else "no_change"
                store.create_evidence(
                    thesis_id=thesis["id"],
                    pillar_id=None,
                    date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    data_point=f"交易复盘: {ticker} 收益 {return_pct:+.1f}%, 持仓 {holding_days} 天",
                    direction=direction,
                    thesis_impact=impact,
                    conviction_before=thesis.get("conviction", "medium"),
                    conviction_after=thesis.get("conviction", "medium"),
                    source="trade_review",
                )
    except Exception as e:
        logger.warning("论点证据更新失败 %s: %s", ticker, e)

    yield _sse("review_done", ticker=ticker, review_id=review_id,
                return_pct=return_pct,
                quality_score=result.get("trade_quality_score", 0),
                lessons=result.get("key_lessons", []),
                message=f"{ticker} 复盘完成：收益 {return_pct:+.1f}%，质量评分 {result.get('trade_quality_score', '?')}/10")


async def run_batch_review(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """批量复盘所有未复盘的卖出交易"""
    unreviewed = store.get_trades_without_review()
    total = len(unreviewed)

    if total == 0:
        yield _sse("batch_review_done", reviewed=0,
                    message="没有待复盘的卖出交易")
        return

    yield _sse("batch_review_start", total=total,
               message=f"开始批量复盘 {total} 笔交易...")

    reviewed = 0
    for trade in unreviewed:
        async for evt in run_trade_review(store, trade["id"], budget):
            yield evt
            if evt.get("data", {}).get("event") == "review_done":
                reviewed += 1

    yield _sse("batch_review_done", reviewed=reviewed, total=total,
               message=f"批量复盘完成：{reviewed}/{total} 笔")


# ─────────────────────────────────────────────────────────
# P3.1 机会成本复盘：扫描"没做的决定"（规则化，无需 LLM）
# ─────────────────────────────────────────────────────────

async def scan_missed_opportunities(
    store: WatchlistStore,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """检测踏空(被拦买入后续上涨) + 错误持有(浮亏未止损)，生成经验卡片。"""
    store = store.for_market(market)
    yield _sse("missed_scan_start", message="开始扫描机会成本...")
    found = 0

    # 1. 踏空：近期被拦/拒的买入计划，标的现价 vs 计划价上涨 >8%
    try:
        blocked = store.get_blocked_executions(limit=30)
        for ep in blocked:
            rj = ep.get("result_json", {})
            if not isinstance(rj, dict):
                continue
            if (rj.get("action") or ep.get("action")) not in ("buy", "add"):
                continue
            ticker = ep.get("ticker", "")
            plan_price = ep.get("target_price") or rj.get("estimated_price", 0)
            snap = store.get_latest_snapshot(ticker)
            cur = snap.get("close") if snap else None
            if not plan_price or not cur:
                continue
            chg = (cur - plan_price) / plan_price * 100
            if chg > 8:
                store.create_experience_card(
                    scope="ticker", scope_key=ticker, category="lesson",
                    title=f"{ticker} 踏空：被拦后上涨 {chg:.0f}%",
                    content=f"该买入计划因风控被拦截，但 {ticker} 现价较计划价 "
                            f"{plan_price:.2f} 上涨 {chg:.1f}%。复盘：风控阈值是否过严，"
                            f"或应在合规规模内建仓而非完全放弃。",
                    evidence=[f"计划价 {plan_price:.2f} → 现价 {cur:.2f}"],
                    confidence=0.4,
                )
                found += 1
    except Exception as e:
        logger.debug("踏空扫描失败: %s", e)

    # 2. 错误持有：持仓浮亏 < -15% 且仍未止损
    try:
        acct = store.get_sim_account()
        positions = store.get_sim_positions(acct.get("id"))
        for p in positions:
            if p.get("shares", 0) <= 0:
                continue
            avg = p.get("avg_cost", 0)
            cur = p.get("current_price", 0)
            if not avg or not cur:
                continue
            pnl_pct = (cur - avg) / avg * 100
            if pnl_pct < -15:
                ticker = p.get("ticker", "")
                store.create_experience_card(
                    scope="ticker", scope_key=ticker, category="lesson",
                    title=f"{ticker} 错误持有：浮亏 {pnl_pct:.0f}% 未止损",
                    content=f"{ticker} 持仓浮亏 {pnl_pct:.1f}%（成本 {avg:.2f}，现价 "
                            f"{cur:.2f}），已超过 -15% 但仍未止损。复盘止损纪律是否执行到位。",
                    evidence=[f"成本 {avg:.2f} → 现价 {cur:.2f}, 浮亏 {pnl_pct:.1f}%"],
                    confidence=0.5,
                )
                found += 1
    except Exception as e:
        logger.debug("错误持有扫描失败: %s", e)

    yield _sse("missed_scan_done", found=found,
               message=f"机会成本扫描完成：发现 {found} 条值得复盘的'没做的决定'")
