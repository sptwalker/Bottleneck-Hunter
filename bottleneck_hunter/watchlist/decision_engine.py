"""四层决策引擎 — L1 宏观策略 + L2 组合策略 + L3 战术 + L4 执行

核心循环：
1. L1 run_macro_strategy / run_macro_check: 宏观环境判断（周度生成 / 日度检查）
2. L2 run_strategic_plan / run_deviation_check: 组合配置（周度生成 / 日度偏离检查）
3. L3 run_tactical_plans: 个股战术计划（日度）
4. L4 run_execution_plans: 具体执行方案（日度）

数据流：strategy_engine.py 输出个股信号 → 本引擎消费 → 组合级决策
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


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt 模板不存在: {path}")


def _get_llm(provider_hint: str | None = None, position: str | None = None):
    """获取 LLM，优先读用户配置 DC_MODEL_<position>，再走 fallback 链"""
    try:
        from bottleneck_hunter.llm_clients.factory import create_llm

        if position:
            env_val = os.environ.get(f"DC_MODEL_{position.upper()}", "").strip()
            if env_val and ":" in env_val:
                p, m = env_val.split(":", 1)
                return create_llm(p, m, temperature=0.3), p, m

        if provider_hint:
            configs = {
                "deepseek": ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
                "qwen": ("qwen", "qwen-plus", "DASHSCOPE_API_KEY"),
                "kimi": ("kimi", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
                "glm": ("glm", "glm-4-flash", "ZHIPU_API_KEY"),
            }
            cfg = configs.get(provider_hint)
            if cfg and os.getenv(cfg[2]):
                return create_llm(cfg[0], cfg[1], temperature=0.3), cfg[0], cfg[1]

        for provider, model, key_env in [
            ("deepseek", "deepseek-chat", "DEEPSEEK_API_KEY"),
            ("qwen", "qwen-plus", "DASHSCOPE_API_KEY"),
            ("kimi", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
            ("glm", "glm-4-flash", "ZHIPU_API_KEY"),
        ]:
            if os.getenv(key_env):
                return create_llm(provider, model, temperature=0.3), provider, model
    except Exception as e:
        logger.warning("无法创建 LLM: %s", e)
    return None, "", ""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────
# 市场上下文
# ─────────────────────────────────────────────────────────

_MARKET_CONTEXT = {
    "a_stock": """## 市场特性（A股）
- 涨跌停限制：主板 ±10%，创业板/科创板 ±20%
- 交易规则：T+1，无做空（融券除外）
- 关键指标：北向资金、融资余额、板块轮动
- 行业分类：申万一级行业
- 止损参考：-7%（涨跌停约束下更严格）
- 估值体系：PE/PB 中枢偏高，需参考行业分位数
- 政策敏感：关注监管政策、产业政策导向""",

    "us_stock": """## 市场特性（美股）
- 无涨跌幅限制（熔断除外）
- 交易规则：T+0，可做空
- 关键指标：VIX、期权 PCR、机构持仓 13F
- 行业分类：GICS 11 大类
- 止损参考：-10%
- 估值体系：DCF 为主，EV/EBITDA、P/S 常用
- 宏观驱动：联储利率决议、非农/CPI 数据""",
}


def _get_market_context_text(markets: list[str] | None = None) -> str:
    """根据观察池涉及的市场生成上下文文本。"""
    if not markets:
        return _MARKET_CONTEXT["us_stock"]
    parts = []
    for m in sorted(set(markets)):
        if m in _MARKET_CONTEXT:
            parts.append(_MARKET_CONTEXT[m])
    return "\n\n".join(parts) if parts else _MARKET_CONTEXT["us_stock"]


# ─────────────────────────────────────────────────────────
# L1: 宏观策略
# ─────────────────────────────────────────────────────────

async def run_macro_strategy(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """生成全新的 L1 宏观策略（通常每周一次）"""
    yield _sse("decision_start", layer="L1", action="generate",
               message="开始生成 L1 宏观策略...")

    llm, provider, model = _get_llm(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=5000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        market_data = await _collect_market_context(store)
        active_markets = market_data.get("markets", [])
        market_ctx = _get_market_context_text(active_markets)
        prompt_template = _load_prompt("decision_macro")
        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{market_indices}", json.dumps(market_data.get("indices", {}), ensure_ascii=False))
                  .replace("{sector_performance}", json.dumps(market_data.get("sectors", {}), ensure_ascii=False))
                  .replace("{sentiment_indicators}", json.dumps(market_data.get("sentiment", {}), ensure_ascii=False))
                  .replace("{macro_economic}", json.dumps(market_data.get("macro", {}), ensure_ascii=False))
                  .replace("{market_news}", json.dumps(market_data.get("news", []), ensure_ascii=False))
                  )

        yield _sse("decision_progress", layer="L1", step="llm_reasoning",
                   message="L1 LLM 推理中...")

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 5000, 2000, "macro_strategy")

        result = extract_json_object(response)
        strategy_id = store.create_macro_strategy(result)

        yield _sse("decision_done", layer="L1", strategy_id=strategy_id,
                   regime=result.get("regime", "sideways"),
                   risk_appetite=result.get("risk_appetite", "balanced"),
                   message=f"L1 宏观策略已生成：{result.get('regime', '?')} / {result.get('risk_appetite', '?')}")

    except Exception as e:
        logger.exception("L1 宏观策略生成失败")
        yield _sse("decision_error", layer="L1", error=str(e))


async def run_macro_check(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """L1 日常检查 — 判断现有宏观策略是否仍然有效"""
    yield _sse("decision_start", layer="L1", action="check",
               message="L1 日常检查中...")

    current = store.get_latest_macro_strategy()
    if not current:
        yield _sse("decision_info", layer="L1",
                   message="无现有 L1 策略，需要先全面生成")
        async for evt in run_macro_strategy(store, budget):
            yield evt
        return

    llm, provider, model = _get_llm(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=2000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        market_data = await _collect_market_context(store)
        active_markets = market_data.get("markets", [])
        market_ctx = _get_market_context_text(active_markets)
        created_at = current.get("created_at", "")
        days_ago = 0
        if created_at:
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - created_dt).days
            except (ValueError, TypeError):
                pass

        prompt_template = _load_prompt("decision_macro_check")
        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{strategy_date}", created_at[:10] if created_at else "未知")
                  .replace("{days_ago}", str(days_ago))
                  .replace("{version}", str(current.get("version", 1)))
                  .replace("{current_strategy}", json.dumps(current.get("result_json", {}), ensure_ascii=False))
                  .replace("{today_market_data}", json.dumps(market_data, ensure_ascii=False))
                  )

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 2000, 500, "macro_check")

        result = extract_json_object(response)
        status = result.get("strategy_status", "valid")

        if status == "needs_major_revision":
            yield _sse("decision_info", layer="L1",
                       message="L1 宏观策略需要重大修订，开始重新生成...")
            async for evt in run_macro_strategy(store, budget):
                yield evt
        else:
            store.update_macro_status(
                current["id"], status,
                minor_tweaks=result.get("minor_tweaks"),
            )
            yield _sse("decision_done", layer="L1", action="check",
                       status=status,
                       commentary=result.get("daily_commentary", ""),
                       message=f"L1 检查完成：{status}")

    except Exception as e:
        logger.exception("L1 日常检查失败")
        yield _sse("decision_error", layer="L1", error=str(e))


# ─────────────────────────────────────────────────────────
# L2: 组合策略
# ─────────────────────────────────────────────────────────

async def run_strategic_plan(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """生成全新的 L2 组合策略"""
    yield _sse("decision_start", layer="L2", action="generate",
               message="开始生成 L2 组合策略...")

    macro = store.get_latest_macro_strategy()
    if not macro:
        yield _sse("decision_info", layer="L2",
                   message="无 L1 宏观策略，需要先生成")
        async for evt in run_macro_strategy(store, budget):
            yield evt
        macro = store.get_latest_macro_strategy()
        if not macro:
            yield _sse("decision_error", layer="L2", error="L1 策略生成失败，无法继续")
            return

    llm, provider, model = _get_llm(position="L2_strategic")
    if not llm:
        yield _sse("decision_error", layer="L2", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L2", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store)
        active_markets = list(store.get_tickers_by_market().keys())
        market_ctx = _get_market_context_text(active_markets)
        account_status = store.get_sim_account()
        positions = store.get_sim_positions(account_status.get("id"))
        previous_plan = store.get_latest_strategic_plan()
        feedback = store.get_rejection_patterns(limit=10)

        lessons = ""
        if feedback:
            lessons = json.dumps(
                [{"ticker": f["ticker"], "reason": f["reason"]} for f in feedback[:5]],
                ensure_ascii=False,
            )

        prompt_template = _load_prompt("decision_strategic")
        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{macro_strategy}", json.dumps(macro.get("result_json", {}), ensure_ascii=False))
                  .replace("{watchlist_signals}", json.dumps(watchlist_signals, ensure_ascii=False))
                  .replace("{account_status}", json.dumps({
                      "total_equity": account_status.get("total_equity", 100000),
                      "cash_balance": account_status.get("cash_balance", 100000),
                      "positions": [{"ticker": p["ticker"], "weight_pct": p.get("weight_pct", 0),
                                     "unrealized_pnl": p.get("unrealized_pnl", 0)} for p in positions],
                  }, ensure_ascii=False))
                  .replace("{lessons_learned}", lessons or "暂无历史复盘数据")
                  .replace("{previous_strategic_plan}", json.dumps(
                      previous_plan.get("result_json", {}) if previous_plan else {},
                      ensure_ascii=False,
                  ))
                  )

        yield _sse("decision_progress", layer="L2", step="llm_reasoning",
                   message="L2 LLM 推理中...")

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 8000, 3000, "strategic_plan")

        result = extract_json_object(response)
        plan_id = store.create_strategic_plan(macro["id"], result)

        yield _sse("decision_done", layer="L2", plan_id=plan_id,
                   stance=result.get("overall_stance", "balanced"),
                   message=f"L2 组合策略已生成：{result.get('overall_stance', '?')}")

    except Exception as e:
        logger.exception("L2 组合策略生成失败")
        yield _sse("decision_error", layer="L2", error=str(e))


async def run_deviation_check(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """L2 偏离检查 — 对比实际持仓与目标策略"""
    yield _sse("decision_start", layer="L2", action="deviation_check",
               message="L2 偏离检查中...")

    plan = store.get_latest_strategic_plan()
    if not plan:
        yield _sse("decision_info", layer="L2",
                   message="无 L2 组合策略，跳过偏离检查")
        return

    llm, provider, model = _get_llm(position="L2_strategic")
    if not llm:
        yield _sse("decision_error", layer="L2", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=3000):
        yield _sse("decision_error", layer="L2", error="预算不足")
        return

    try:
        account = store.get_sim_account()
        positions = store.get_sim_positions(account.get("id"))
        positions_data = []
        for p in positions:
            positions_data.append({
                "ticker": p["ticker"],
                "shares": p.get("shares", 0),
                "market_value": p.get("market_value", 0),
                "weight_pct": p.get("weight_pct", 0),
                "unrealized_pnl": p.get("unrealized_pnl", 0),
            })

        prompt_template = _load_prompt("decision_deviation_check")
        prompt = (prompt_template
                  .replace("{strategic_plan}", json.dumps(plan.get("result_json", {}), ensure_ascii=False))
                  .replace("{current_positions}", json.dumps({
                      "total_equity": account.get("total_equity", 100000),
                      "cash_balance": account.get("cash_balance", 100000),
                      "cash_pct": round(account.get("cash_balance", 100000)
                                        / max(account.get("total_equity", 100000), 1) * 100, 1),
                      "positions": positions_data,
                  }, ensure_ascii=False))
                  )

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 3000, 800, "deviation_check")

        result = extract_json_object(response)
        rebalance_needed = result.get("rebalance_needed", False)

        yield _sse("decision_done", layer="L2", action="deviation_check",
                   rebalance_needed=rebalance_needed,
                   deviation_pct=result.get("overall_deviation_pct", 0),
                   commentary=result.get("commentary", ""),
                   message=f"L2 偏离检查完成：{'需要调仓' if rebalance_needed else '在容忍范围内'}")

    except Exception as e:
        logger.exception("L2 偏离检查失败")
        yield _sse("decision_error", layer="L2", error=str(e))


# ─────────────────────────────────────────────────────────
# L3: 战术计划
# ─────────────────────────────────────────────────────────

async def run_tactical_plans(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """生成 L3 战术计划 — 每只目标股票的买卖时机"""
    yield _sse("decision_start", layer="L3", action="generate",
               message="开始生成 L3 战术计划...")

    strategic = store.get_latest_strategic_plan()
    if not strategic:
        yield _sse("decision_error", layer="L3", error="无 L2 组合策略，无法生成战术计划")
        return

    macro = store.get_latest_macro_strategy()
    if not macro:
        yield _sse("decision_error", layer="L3", error="无 L1 宏观策略")
        return

    llm, provider, model = _get_llm(position="L3_tactical")
    if not llm:
        yield _sse("decision_error", layer="L3", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L3", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store)
        active_markets = list(store.get_tickers_by_market().keys())
        market_ctx = _get_market_context_text(active_markets)
        catalysts = store.get_upcoming_catalysts(days=30)
        catalyst_by_ticker = {}
        for c in catalysts:
            catalyst_by_ticker.setdefault(c["ticker"], []).append({
                "title": c.get("title", ""),
                "type": c.get("catalyst_type", ""),
                "expected_date": c.get("expected_date", ""),
                "impact_level": c.get("impact_level", "medium"),
                "confidence": c.get("confidence", 5),
            })

        stock_data = []
        entries = store.list_all()
        for entry in entries:
            ticker = entry["ticker"]
            snap = store.get_latest_snapshot(ticker)
            signal = next((s for s in watchlist_signals if s["ticker"] == ticker), {})
            stock_data.append({
                "ticker": ticker,
                "company_name": entry.get("company_name", ticker),
                "sector": entry.get("sector", ""),
                "tier": entry.get("tier", "track"),
                "signal": signal.get("signal", "neutral"),
                "confidence": signal.get("confidence", 5),
                "price": snap.get("close") if snap else None,
                "change_pct": snap.get("change_pct") if snap else None,
                "rsi_14": snap.get("rsi_14") if snap else None,
                "sma_50": snap.get("sma_50") if snap else None,
                "volume": snap.get("volume") if snap else None,
            })

        prompt_template = _load_prompt("decision_tactical")
        macro_text = macro.get("market_summary", "") or json.dumps(
            macro.get("result_json", {}), ensure_ascii=False)[:500]
        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{macro_summary}", macro_text)
                  .replace("{strategic_plan}", json.dumps(strategic.get("result_json", {}), ensure_ascii=False))
                  .replace("{stock_data}", json.dumps(stock_data, ensure_ascii=False))
                  .replace("{catalyst_timeline}", json.dumps(catalyst_by_ticker, ensure_ascii=False))
                  )

        yield _sse("decision_progress", layer="L3", step="llm_reasoning",
                   message="L3 LLM 推理中...")

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 8000, 3000, "tactical_plans")

        result = extract_json_object(response)
        tactical_plans = result.get("tactical_plans", [])

        entry_map = {e["ticker"]: e["id"] for e in entries}
        plan_ids = []
        for tp in tactical_plans:
            ticker = tp.get("ticker", "")
            entry_id = entry_map.get(ticker, "")
            if not ticker:
                continue
            plan_id = store.create_tactical_plan(
                strategic_plan_id=strategic["id"],
                entry_id=entry_id,
                ticker=ticker,
                plan_date=_today(),
                result_json=tp,
            )
            plan_ids.append(plan_id)

        yield _sse("decision_done", layer="L3",
                   plan_count=len(plan_ids),
                   priority_ranking=result.get("priority_ranking", []),
                   message=f"L3 战术计划已生成：{len(plan_ids)} 只股票")

    except Exception as e:
        logger.exception("L3 战术计划生成失败")
        yield _sse("decision_error", layer="L3", error=str(e))


# ─────────────────────────────────────────────────────────
# L4: 执行方案
# ─────────────────────────────────────────────────────────

async def run_execution_plans(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """生成 L4 执行方案 — 可执行操作序列"""
    yield _sse("decision_start", layer="L4", action="generate",
               message="开始生成 L4 执行方案...")

    tactical_plans = store.get_tactical_plans_by_date(_today())
    if not tactical_plans:
        yield _sse("decision_info", layer="L4",
                   message="今日无 L3 战术计划，跳过 L4")
        return

    actionable = [tp for tp in tactical_plans if tp.get("action") != "hold"]
    if not actionable:
        yield _sse("decision_done", layer="L4",
                   message="L3 计划全部为持有，无需生成执行方案")
        return

    llm, provider, model = _get_llm(position="L4_execution")
    if not llm:
        yield _sse("decision_error", layer="L4", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=5000):
        yield _sse("decision_error", layer="L4", error="预算不足")
        return

    try:
        active_markets = list(store.get_tickers_by_market().keys())
        market_ctx = _get_market_context_text(active_markets)
        account = store.get_sim_account()
        positions = store.get_sim_positions(account.get("id"))
        feedback = store.get_rejection_patterns(limit=10)
        preferences = store.get_preferences()

        cash_balance = account.get("cash_balance", 100000)

        prompt_template = _load_prompt("decision_execution")
        tactical_json = json.dumps(
            [tp.get("result_json", tp) for tp in actionable], ensure_ascii=False)
        account_json = json.dumps({
            "total_equity": account.get("total_equity", 100000),
            "cash_balance": cash_balance,
            "positions": [{"ticker": p["ticker"], "shares": p.get("shares", 0),
                           "avg_cost": p.get("avg_cost", 0),
                           "market_value": p.get("market_value", 0),
                           "weight_pct": p.get("weight_pct", 0),
                           "unrealized_pnl": p.get("unrealized_pnl", 0)}
                          for p in positions],
        }, ensure_ascii=False)
        feedback_text = (json.dumps(
            [{"ticker": f.get("ticker", ""), "reason": f.get("reason", "")}
             for f in feedback[:5]], ensure_ascii=False)
            if feedback else "暂无历史拒绝记录")
        pref_text = (json.dumps(
            {p["key"]: p["value"] for p in preferences}, ensure_ascii=False)
            if preferences else "暂无用户偏好")

        tickers_in_play = [tp.get("ticker", "") for tp in actionable if tp.get("ticker")]
        experience_text = "暂无历史经验"
        applied_card_ids = []
        if tickers_in_play:
            all_cards = []
            for tk in tickers_in_play:
                entry = next((e for e in store.list_all() if e["ticker"] == tk), {})
                sector = entry.get("sector", "")
                cards = store.get_relevant_cards(tk, sector, limit=3)
                for c in cards:
                    if c["id"] not in [ac["id"] for ac in all_cards]:
                        all_cards.append(c)
            if all_cards:
                experience_text = json.dumps(
                    [{"title": c["title"], "content": c["content"],
                      "scope": c["scope"], "confidence": c["confidence"]}
                     for c in all_cards[:8]], ensure_ascii=False)
                applied_card_ids = [c["id"] for c in all_cards[:8]]

        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{tactical_plans}", tactical_json)
                  .replace("{account_status}", account_json)
                  .replace("{available_cash}", f"{cash_balance:,.0f}")
                  .replace("{trade_feedback}", feedback_text)
                  .replace("{user_preferences}", pref_text)
                  .replace("{experience_cards}", experience_text)
                  )

        yield _sse("decision_progress", layer="L4", step="llm_reasoning",
                   message="L4 LLM 推理中...")

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 5000, 2000, "execution_plans")

        result = extract_json_object(response)
        exec_plans = result.get("execution_plans", [])

        entry_map = {e["ticker"]: e["id"] for e in store.list_all()}
        tactical_map = {tp["ticker"]: tp["id"] for tp in actionable}
        created_ids = []

        for ep in exec_plans:
            ticker = ep.get("ticker", "")
            if not ticker:
                continue
            entry_id = entry_map.get(ticker, "")
            tactical_id = tactical_map.get(ticker, "")
            plan_id = store.create_execution_plan(
                tactical_plan_id=tactical_id,
                entry_id=entry_id,
                ticker=ticker,
                result_json=ep,
            )
            created_ids.append(plan_id)

        for cid in applied_card_ids:
            store.increment_card_applied(cid)

        yield _sse("decision_done", layer="L4",
                   plan_count=len(created_ids),
                   execution_summary=result.get("execution_summary", {}),
                   skipped=result.get("skipped_plans", []),
                   message=f"L4 执行方案已生成：{len(created_ids)} 条待确认操作")

    except Exception as e:
        logger.exception("L4 执行方案生成失败")
        yield _sse("decision_error", layer="L4", error=str(e))


# ─────────────────────────────────────────────────────────
# 完整日常决策流程
# ─────────────────────────────────────────────────────────

async def run_daily_decision(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    scope: str = "full",
) -> AsyncGenerator[dict, None]:
    """完整日常决策流程：L1→L2→L3→L4→投委会

    scope: "l1" = 仅 L1 检查
           "l3l4" = 仅 L3-L4 更新
           "full" = 全流程
    """
    yield _sse("daily_start", scope=scope, message="开始日常决策流程...")

    # Step 0: 催化剂时效检查
    from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry
    async for evt in check_catalyst_expiry(store):
        yield evt

    # Step 1: L1 宏观检查
    if scope in ("l1", "full"):
        async for evt in run_macro_check(store, budget):
            yield evt

    # Step 2: L2 偏离检查
    if scope in ("full",):
        macro = store.get_latest_macro_strategy()
        plan = store.get_latest_strategic_plan()

        if not plan and macro:
            yield _sse("decision_info", layer="L2",
                       message="无 L2 组合策略，自动生成...")
            async for evt in run_strategic_plan(store, budget):
                yield evt
        elif plan:
            async for evt in run_deviation_check(store, budget):
                yield evt

    if scope == "l1":
        yield _sse("daily_done", message="L1 检查完成")
        return

    # Step 3: L3 战术计划
    if scope in ("l3l4", "full"):
        async for evt in run_tactical_plans(store, budget):
            yield evt

    # Step 4: L4 执行方案
    if scope in ("l3l4", "full"):
        async for evt in run_execution_plans(store, budget):
            yield evt

    # Step 5: 投委会评审
    if scope in ("l3l4", "full"):
        pending = store.get_pending_executions()
        if pending:
            from bottleneck_hunter.watchlist.committee import run_committee_review
            yield _sse("decision_info", layer="committee",
                       message=f"启动投委会评审 {len(pending)} 条执行计划...")
            async for evt in run_committee_review(store, pending, budget):
                yield evt
        else:
            yield _sse("decision_info", layer="committee",
                       message="无待评审执行计划，跳过投委会")

    yield _sse("daily_done", message="日常决策流程完成")


async def run_full_refresh(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """全量刷新：重新生成 L1 + L2 + L3 + L4 + 投委会"""
    yield _sse("refresh_start", message="开始全量决策刷新...")

    async for evt in run_macro_strategy(store, budget):
        yield evt

    async for evt in run_strategic_plan(store, budget):
        yield evt

    async for evt in run_tactical_plans(store, budget):
        yield evt

    async for evt in run_execution_plans(store, budget):
        yield evt

    pending = store.get_pending_executions()
    if pending:
        from bottleneck_hunter.watchlist.committee import run_committee_review
        async for evt in run_committee_review(store, pending, budget):
            yield evt

    yield _sse("refresh_done", message="全量决策刷新完成")


# ─────────────────────────────────────────────────────────
# 数据收集辅助
# ─────────────────────────────────────────────────────────

async def _collect_market_context(store: WatchlistStore) -> dict:
    """收集市场宏观数据（从观察池数据中聚合），并附带市场类型列表。"""
    by_market = store.get_tickers_by_market()
    active_markets = [m for m, ts in by_market.items() if ts]
    tickers = [t for ts in by_market.values() for t in ts]

    all_snapshots = []
    for ticker in tickers:
        snap = store.get_latest_snapshot(ticker)
        if snap:
            all_snapshots.append(snap)

    if not all_snapshots:
        return {"indices": {}, "sectors": {}, "sentiment": {}, "macro": {},
                "news": [], "markets": active_markets}

    avg_change = sum(s.get("change_pct", 0) or 0 for s in all_snapshots) / max(len(all_snapshots), 1)
    avg_rsi = sum(s.get("rsi_14", 50) or 50 for s in all_snapshots) / max(len(all_snapshots), 1)

    entries = store.list_all()
    sectors = {}
    for entry in entries:
        sector = entry.get("sector", "未分类")
        if sector not in sectors:
            sectors[sector] = {"tickers": [], "avg_change": 0}
        sectors[sector]["tickers"].append(entry["ticker"])

    for sector, info in sectors.items():
        changes = []
        for t in info["tickers"]:
            snap = store.get_latest_snapshot(t)
            if snap and snap.get("change_pct") is not None:
                changes.append(snap["change_pct"])
        info["avg_change"] = round(sum(changes) / max(len(changes), 1), 2) if changes else 0
        info["count"] = len(info["tickers"])
        del info["tickers"]

    news_items = []
    for ticker in tickers[:5]:
        recent = store.get_news(ticker, limit=2)
        for n in recent:
            news_items.append({"ticker": ticker, "title": n.get("title", ""),
                               "sentiment": n.get("sentiment", "")})

    return {
        "indices": {
            "watchlist_avg_change_pct": round(avg_change, 2),
            "watchlist_avg_rsi": round(avg_rsi, 1),
            "stocks_tracked": len(all_snapshots),
        },
        "sectors": sectors,
        "sentiment": {
            "avg_rsi": round(avg_rsi, 1),
            "stocks_above_sma50": sum(
                1 for s in all_snapshots
                if s.get("close") and s.get("sma_50") and s["close"] > s["sma_50"]
            ),
            "stocks_total": len(all_snapshots),
        },
        "macro": {},
        "news": news_items[:10],
        "markets": active_markets,
    }


def _collect_watchlist_signals(store: WatchlistStore) -> list[dict]:
    """从已有的 strategy_records 收集个股信号"""
    entries = store.list_all()
    signals = []

    strategy_summaries = store.get_all_strategy_summaries()

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]
        summary = strategy_summaries.get(entry_id, {})

        snap = store.get_latest_snapshot(ticker)

        signals.append({
            "ticker": ticker,
            "company_name": entry.get("company_name", ticker),
            "sector": entry.get("sector", ""),
            "tier": entry.get("tier", "track"),
            "signal": summary.get("signal", "neutral"),
            "confidence": summary.get("confidence", 5),
            "price": snap.get("close") if snap else None,
            "change_pct": snap.get("change_pct") if snap else None,
            "rsi_14": snap.get("rsi_14") if snap else None,
        })

    return signals
