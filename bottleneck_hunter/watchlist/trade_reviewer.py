"""交易复盘引擎 — 卖出交易后的 LLM 归因分析

对已完成的卖出交易进行自动复盘：对比入场逻辑与实际结果，
提取经验教训，生成经验卡片供后续决策参考。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.chain.json_utils import extract_json_object

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": {"event": event, **data}}


def _get_llm():
    try:
        from bottleneck_hunter.llm_clients.factory import create_llm
        env_val = os.environ.get("DC_MODEL_L4_EXECUTION", "").strip()
        if env_val and ":" in env_val:
            p, m = env_val.split(":", 1)
            return create_llm(p, m, temperature=0.2), p, m

        for provider, model, key_env in [
            ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
            ("qwen", "qwen-plus", "DASHSCOPE_API_KEY"),
            ("kimi", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
            ("glm", "glm-4-flash", "ZHIPU_API_KEY"),
        ]:
            if os.getenv(key_env):
                return create_llm(provider, model, temperature=0.2), provider, model
    except Exception as e:
        logger.warning("无法创建 LLM: %s", e)
    return None, "", ""


def _rule_based_review(
    ticker: str, entry_price: float, exit_price: float,
    return_pct: float, holding_days: int,
) -> dict:
    """基于规则的简易复盘 — 当 LLM 不可用或调用失败时的 fallback。

    不依赖 LLM，仅根据收益率和持仓天数给出基本评价和经验教训。
    """
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
    catalyst_status = json.dumps(
        [{"title": c.get("title", ""), "status": c.get("status", ""),
          "expected_date": c.get("expected_date", "")} for c in catalysts[:5]],
        ensure_ascii=False) if catalysts else "无相关催化剂"

    # ── 尝试 LLM 复盘，失败则降级为规则复盘 ──
    use_fallback = False
    fallback_reason = ""

    llm, provider, model = _get_llm()
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
