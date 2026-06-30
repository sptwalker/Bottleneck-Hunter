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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.regime_mapper import get_allocation_bounds, format_bounds_for_prompt
from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.llm_clients.factory import get_llm_for_position, get_models_for_role

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": {"event": event, **data}}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt 模板不存在: {path}")


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


def _merge_macro_results(results: list[dict]) -> dict:
    """合并多个 L1 宏观策略结果 — 多数投票 + 加权均值。"""
    if len(results) == 1:
        return results[0]

    from collections import Counter

    regimes = [r.get("regime", "sideways") for r in results]
    regime = Counter(regimes).most_common(1)[0][0]

    appetites = [r.get("risk_appetite", "balanced") for r in results]
    risk_appetite = Counter(appetites).most_common(1)[0][0]

    confidences = [r.get("regime_confidence", 5) for r in results]
    avg_confidence = round(sum(confidences) / len(confidences), 1)

    base = results[0].copy()
    base["regime"] = regime
    base["risk_appetite"] = risk_appetite
    base["regime_confidence"] = avg_confidence

    if any(r.get("regime") != regime for r in results):
        base["_divergence_warning"] = f"模型分歧: regime 判断不一致 ({regimes})"

    return base


# ─────────────────────────────────────────────────────────
# L1: 宏观策略
# ─────────────────────────────────────────────────────────

async def run_macro_strategy(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成全新的 L1 宏观策略（通常每周一次）"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L1", action="generate",
               market=market, message="开始生成 L1 宏观策略...")

    llm, provider, model = get_llm_for_position(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=5000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        market_data = await _collect_market_context(store, market)
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

        all_models = get_models_for_role("L1_macro")
        use_cross = len(all_models) >= 2

        if use_cross:
            yield _sse("decision_progress", layer="L1", step="llm_reasoning",
                       message=f"L1 双模型交叉验证中... ({len(all_models)} 路)")

            async def _invoke_model(m_llm, m_prov, m_mod):
                r = await asyncio.to_thread(lambda: m_llm.invoke(prompt).content)
                if budget:
                    budget.record(m_prov, m_mod, 5000, 2000, "macro_strategy")
                return extract_json_object(r), m_prov, m_mod

            tasks = [_invoke_model(*m) for m in all_models[:2]]
            results_raw = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = [item for item in results_raw if not isinstance(item, Exception)]

            if len(valid_results) >= 2:
                result = _merge_macro_results([v[0] for v in valid_results])
                result["_cross_validated"] = True
                result["_models_used"] = [f"{v[1]}:{v[2]}" for v in valid_results]
                logger.info("L1 宏观策略双模型交叉验证完成: regime=%s", result.get("regime"))
            elif valid_results:
                result = valid_results[0][0]
            else:
                raise RuntimeError("所有模型调用均失败")
        else:
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
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """L1 日常检查 — 判断现有宏观策略是否仍然有效"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L1", action="check",
               message="L1 日常检查中...")

    current = store.get_latest_macro_strategy()
    if not current:
        yield _sse("decision_info", layer="L1",
                   message="无现有 L1 策略，需要先全面生成")
        async for evt in run_macro_strategy(store, budget, market=market):
            yield evt
        return

    llm, provider, model = get_llm_for_position(position="L1_macro")
    if not llm:
        yield _sse("decision_error", layer="L1", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=2000):
        yield _sse("decision_error", layer="L1", error="预算不足")
        return

    try:
        market_data = await _collect_market_context(store, market)
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
            async for evt in run_macro_strategy(store, budget, market=market):
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
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成全新的 L2 组合策略"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L2", action="generate",
               message="开始生成 L2 组合策略...")

    macro = store.get_latest_macro_strategy()
    if not macro:
        yield _sse("decision_info", layer="L2",
                   message="无 L1 宏观策略，需要先生成")
        async for evt in run_macro_strategy(store, budget, market=market):
            yield evt
        macro = store.get_latest_macro_strategy()
        if not macro:
            yield _sse("decision_error", layer="L2", error="L1 策略生成失败，无法继续")
            return

    llm, provider, model = get_llm_for_position(position="L2_strategic")
    if not llm:
        yield _sse("decision_error", layer="L2", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L2", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store, market)
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

        macro_json = macro.get("result_json", {})
        regime = macro_json.get("regime", "sideways")
        risk_appetite = macro_json.get("risk_appetite", "balanced")
        confidence = macro_json.get("regime_confidence", 5)
        alloc_bounds = get_allocation_bounds(regime, risk_appetite, confidence)
        bounds_text = format_bounds_for_prompt(alloc_bounds)

        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{macro_strategy}", json.dumps(macro_json, ensure_ascii=False))
                  .replace("{allocation_bounds}", bounds_text)
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

        # Phase 20D: 解析并保存三场景估值
        try:
            stock_selection = result.get("stock_selection", {})
            entry_map = {e["ticker"]: e["id"] for e in store.list_all() if e.get("market") == market}
            for holding in (stock_selection.get("core_holdings", []) +
                            stock_selection.get("tactical_holdings", [])):
                sv = holding.get("scenario_valuation")
                if not sv:
                    continue
                ticker = holding.get("ticker", "")
                entry_id = entry_map.get(ticker, "")
                if not entry_id:
                    continue
                snap = store.get_latest_snapshot(ticker)
                current_price = snap.get("close", 0) if snap else 0
                store.create_scenario_valuation(
                    entry_id=entry_id,
                    ticker=ticker,
                    strategic_plan_id=plan_id,
                    bear_price=sv.get("bear_price", 0),
                    bear_probability=sv.get("bear_probability", 20),
                    bear_rationale=sv.get("bear_rationale", ""),
                    base_price=sv.get("base_price", 0),
                    base_probability=sv.get("base_probability", 60),
                    base_rationale=sv.get("base_rationale", ""),
                    bull_price=sv.get("bull_price", 0),
                    bull_probability=sv.get("bull_probability", 20),
                    bull_rationale=sv.get("bull_rationale", ""),
                    current_price=current_price,
                    valuation_method=sv.get("valuation_method", "relative"),
                )
        except Exception as e:
            logger.warning("场景估值保存失败: %s", e)

        yield _sse("decision_done", layer="L2", plan_id=plan_id,
                   stance=result.get("overall_stance", "balanced"),
                   message=f"L2 组合策略已生成：{result.get('overall_stance', '?')}")

    except Exception as e:
        logger.exception("L2 组合策略生成失败")
        yield _sse("decision_error", layer="L2", error=str(e))


async def run_deviation_check(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """L2 偏离检查 — 对比实际持仓与目标策略"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L2", action="deviation_check",
               message="L2 偏离检查中...")

    plan = store.get_latest_strategic_plan()
    if not plan:
        yield _sse("decision_info", layer="L2",
                   message="无 L2 组合策略，跳过偏离检查")
        return

    llm, provider, model = get_llm_for_position(position="L2_strategic")
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
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成 L3 战术计划 — 每只目标股票的买卖时机"""
    store = store.for_market(market)
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

    llm, provider, model = get_llm_for_position(position="L3_tactical")
    if not llm:
        yield _sse("decision_error", layer="L3", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=8000):
        yield _sse("decision_error", layer="L3", error="预算不足")
        return

    try:
        watchlist_signals = _collect_watchlist_signals(store, market)
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

        # P1.1 已判定催化剂(realized/failed/partial) → 买卖信号
        judged = store.get_recently_judged_catalysts(days=7)
        outcome_by_ticker = {}
        catalyst_outcome_tickers = set()
        for c in judged:
            tk = c.get("ticker", "")
            if not tk:
                continue
            outcome_by_ticker.setdefault(tk, []).append({
                "title": c.get("title", ""),
                "outcome": c.get("outcome", ""),
                "impact": c.get("outcome_impact", 0),
                "judged_at": (c.get("judged_at", "") or "")[:10],
            })
            catalyst_outcome_tickers.add(tk)

        stock_data = []
        entries = store.list_all()
        entries = [e for e in entries if e.get("market") == market]

        # 19C: 基于 L2 stock_selection 过滤，确保 L3 只为 L2 选定的标的生成战术计划
        strategic_json = strategic.get("result_json", {})
        stock_selection = strategic_json.get("stock_selection", {})
        core_holdings = stock_selection.get("core_holdings", [])
        tactical_holdings = stock_selection.get("tactical_holdings", [])
        core_tickers = {s.get("ticker", "") for s in core_holdings if s.get("ticker")}
        tactical_tickers = {s.get("ticker", "") for s in tactical_holdings if s.get("ticker")}
        watch_tickers = set(stock_selection.get("watchlist_only", []))
        selected_tickers = core_tickers | tactical_tickers | watch_tickers

        # P1.1 强制纳入：持仓中且催化剂已落空的标的(即使不在 L2 选股)，确保能生成止损/减仓战术
        held_tickers = {p["ticker"] for p in store.get_sim_positions(
            store.get_sim_account().get("id")) if p.get("shares", 0) > 0}
        forced = (catalyst_outcome_tickers & held_tickers)
        if forced:
            selected_tickers = selected_tickers | forced

        if selected_tickers:
            entries = [e for e in entries if e["ticker"] in selected_tickers]
            if not entries:
                logger.warning("L2 选股 %s 未匹配到观察池标的，降级为全量处理",
                               selected_tickers)
                entries = store.list_all()

        for entry in entries:
            ticker = entry["ticker"]
            snap = store.get_latest_snapshot(ticker)
            signal = next((s for s in watchlist_signals if s["ticker"] == ticker), {})

            l2_role = "core" if ticker in core_tickers else "tactical" if ticker in tactical_tickers else "watch"
            l2_target_weight = 0
            for s in core_holdings + tactical_holdings:
                if s.get("ticker") == ticker:
                    l2_target_weight = s.get("target_weight_pct", 0)
                    break

            stock_data.append({
                "ticker": ticker,
                "company_name": entry.get("company_name", ticker),
                "sector": entry.get("sector", ""),
                "tier": entry.get("tier", "track"),
                "l2_role": l2_role,
                "l2_target_weight": l2_target_weight,
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

        recent_map = _recent_executed_by_ticker(store)
        recent_trades_text = "暂无近期已执行交易"
        if recent_map:
            recent_list = []
            for tk, trades in recent_map.items():
                for tr in trades:
                    recent_list.append(f"{tk} {tr['side']} {tr['shares']}股 ({tr['date']})")
            recent_trades_text = "\n".join(recent_list)

        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{macro_summary}", macro_text)
                  .replace("{strategic_plan}", json.dumps(strategic.get("result_json", {}), ensure_ascii=False))
                  .replace("{stock_data}", json.dumps(stock_data, ensure_ascii=False))
                  .replace("{catalyst_timeline}", json.dumps(catalyst_by_ticker, ensure_ascii=False))
                  .replace("{catalyst_outcomes}",
                           json.dumps(outcome_by_ticker, ensure_ascii=False) if outcome_by_ticker else "暂无已判定催化剂")
                  .replace("{recent_trades}", recent_trades_text)
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

# 执行去重：5天冷却窗口
EXECUTION_COOLDOWN_DAYS = 5
_BUY_FAMILY = {"buy", "add"}
_SELL_FAMILY = {"sell", "reduce"}

def _recent_executed_by_ticker(store, days=EXECUTION_COOLDOWN_DAYS) -> dict[str, list[dict]]:
    """返回 {ticker: [{side, shares, date}]}，仅含近 days 天已执行的 sim_trades。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    trades = store.get_sim_trades(limit=200)
    out = {}
    for t in trades:
        if (t.get("created_at", "") or "") < cutoff:
            continue
        out.setdefault(t["ticker"], []).append({
            "side": t.get("side", ""),
            "shares": t.get("shares", 0),
            "date": (t.get("created_at", "") or "")[:10],
        })
    return out

def _is_recent_duplicate(action, ticker, recent_map) -> bool:
    """该 ticker 的同向操作族近期是否已执行过。"""
    fam = _BUY_FAMILY if action in _BUY_FAMILY else _SELL_FAMILY if action in _SELL_FAMILY else set()
    for t in recent_map.get(ticker, []):
        if t["side"] in fam:
            return True
    return False

def _repair_execution_plan(llm, ep: dict, violations: list[str],
                           account: dict, constraints: dict) -> dict | None:
    """P0.2 LLM 自修正：带违规详情重新生成单个执行计划。

    返回修正后的 ep dict；若 LLM 判定不可行或调用失败，返回 None。
    """
    try:
        template = _load_prompt("decision_execution_repair")
        prompt = (template
                  .replace("{original_plan}", json.dumps(ep, ensure_ascii=False))
                  .replace("{violations}", "\n".join(f"- {v}" for v in violations))
                  .replace("{account_status}", json.dumps({
                      "total_equity": account.get("total_equity", 100000),
                      "cash_balance": account.get("cash_balance", 0),
                  }, ensure_ascii=False))
                  .replace("{constraints}", json.dumps(constraints, ensure_ascii=False)))
        response = llm.invoke(prompt).content
        fixed = extract_json_object(response)
        if not fixed or not fixed.get("feasible", False):
            return None
        # 合并修正字段回原计划
        ep = dict(ep)
        if fixed.get("shares") is not None:
            ep["shares"] = fixed["shares"]
        if fixed.get("estimated_price") is not None:
            ep["estimated_price"] = fixed["estimated_price"]
            ep["target_price"] = fixed["estimated_price"]
        if fixed.get("execution_method"):
            ep["execution_method"] = fixed["execution_method"]
        ep["estimated_amount"] = (ep.get("shares", 0) or 0) * (fixed.get("estimated_price")
                                                               or ep.get("estimated_price", 0) or 0)
        ep["auto_repaired"] = True
        ep["repair_note"] = fixed.get("adjustment_note", "")
        return ep
    except Exception as e:
        logger.warning("执行计划自修正失败: %s", e)
        return None


async def run_execution_plans(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """生成 L4 执行方案 — 可执行操作序列"""
    store = store.for_market(market)
    yield _sse("decision_start", layer="L4", action="generate",
               message="开始生成 L4 执行方案...")

    tactical_plans = store.get_tactical_plans_by_date(_today())
    if not tactical_plans:
        yield _sse("decision_info", layer="L4",
                   message="今日无 L3 战术计划，跳过 L4")
        return

    actionable = [tp for tp in tactical_plans if tp.get("action") not in ("hold", "wait_for_pullback")]
    if not actionable:
        yield _sse("decision_done", layer="L4",
                   message="L3 计划全部为持有，无需生成执行方案")
        return

    llm, provider, model = get_llm_for_position(position="L4_execution")
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

        layer_perf = store.get_layer_performance_summary()
        layer_perf_text = (json.dumps(layer_perf, ensure_ascii=False)
                           if layer_perf else "暂无分层绩效数据")

        recent_map = _recent_executed_by_ticker(store)
        recent_trades_text = "暂无近期已执行交易"
        if recent_map:
            recent_list = []
            for tk, trades in recent_map.items():
                for tr in trades:
                    recent_list.append(f"{tk} {tr['side']} {tr['shares']}股 ({tr['date']})")
            recent_trades_text = "\n".join(recent_list)

        prompt = (prompt_template
                  .replace("{market_context}", market_ctx)
                  .replace("{tactical_plans}", tactical_json)
                  .replace("{account_status}", account_json)
                  .replace("{available_cash}", f"{cash_balance:,.0f}")
                  .replace("{trade_feedback}", feedback_text)
                  .replace("{recent_trades}", recent_trades_text)
                  .replace("{user_preferences}", pref_text)
                  .replace("{experience_cards}", experience_text)
                  .replace("{layer_performance}", layer_perf_text)
                  )

        yield _sse("decision_progress", layer="L4", step="llm_reasoning",
                   message="L4 LLM 推理中...")

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 5000, 2000, "execution_plans")

        result = extract_json_object(response)
        exec_plans = result.get("execution_plans", [])

        entry_map = {e["ticker"]: e["id"] for e in store.list_all() if e.get("market") == market}
        sector_map = {e["ticker"]: e.get("sector", "") for e in store.list_all() if e.get("market") == market}
        tactical_map = {tp["ticker"]: tp["id"] for tp in actionable}
        created_ids = []
        skipped = 0
        blocked = 0
        repaired = 0

        # P0.6 动态约束：按 L1 风险偏好选择约束集
        from bottleneck_hunter.watchlist.constraint_validator import (
            validate_execution_plan, max_compliant_shares, get_constraints_for_appetite,
            validate_portfolio_beta)
        macro = store.get_latest_macro_strategy()
        risk_appetite = (macro or {}).get("risk_appetite", "")
        constraints = get_constraints_for_appetite(risk_appetite)

        # P2.1 构建 beta_map(从 company_profiles，缺失则降级)
        beta_map = {}
        for tk in set(list(entry_map.keys()) + [p["ticker"] for p in positions]):
            try:
                prof = store.get_company_profile(tk)
                b = (prof or {}).get("raw", {}).get("beta")
                if b is not None:
                    beta_map[tk] = float(b)
            except Exception:
                pass

        existing_tickers = {ep["ticker"] for ep in store.get_pending_executions() if ep.get("ticker")}
        batch_tickers = set()
        recent_map = _recent_executed_by_ticker(store)

        for ep in exec_plans:
            ticker = ep.get("ticker", "")
            if not ticker:
                continue
            if ticker in existing_tickers:
                logger.info("跳过已有 pending 执行计划的 %s", ticker)
                skipped += 1
                continue
            if ticker in batch_tickers:
                logger.info("跳过本批次重复的 %s", ticker)
                skipped += 1
                continue
            if _is_recent_duplicate(ep.get("action", ""), ticker, recent_map):
                logger.info("跳过近期已执行同向操作的 %s (%s)", ticker, ep.get("action"))
                skipped += 1
                continue
            batch_tickers.add(ticker)
            entry_id = entry_map.get(ticker, "")
            tactical_id = tactical_map.get(ticker, "")
            ep["applied_card_ids"] = applied_card_ids
            ep.setdefault("market", market)
            ep.setdefault("sector", sector_map.get(ticker, ""))

            # ── P0.1 前置约束校验 + P2.1 组合 beta 校验 ──
            def _full_validate(plan_ep):
                vr = validate_execution_plan(plan_ep, account, positions, constraints)
                br = validate_portfolio_beta(plan_ep, account, positions, beta_map, constraints)
                if not br.valid:
                    vr.violations.extend(br.violations)
                    vr.valid = False
                return vr

            vres = _full_validate(ep)

            if not vres.valid:
                # ── P0.2 LLM 自修正（最多 2 轮）──
                for _ in range(2):
                    fixed = await asyncio.to_thread(
                        _repair_execution_plan, llm, ep, vres.violations, account, constraints)
                    if fixed is None:
                        break
                    fixed.setdefault("market", market)
                    fixed.setdefault("sector", sector_map.get(ticker, ""))
                    vres2 = _full_validate(fixed)
                    if vres2.valid:
                        ep = fixed
                        vres = vres2
                        repaired += 1
                        break
                    ep, vres = fixed, vres2

            if not vres.valid:
                # ── P0.3 自动降级：缩量到合规 ──
                n = max_compliant_shares(ep, account, positions, constraints)
                if n > 0 and ep.get("action") in ("buy", "add"):
                    ep["shares"] = n
                    price = ep.get("target_price") or ep.get("estimated_price", 0)
                    ep["estimated_amount"] = n * price
                    ep["auto_adjusted"] = True
                    vres = _full_validate(ep)

            if not vres.valid:
                # ── P0.3 无法降级：拦截，写入"已拦截"区 + 回灌反馈 ──
                store.create_blocked_execution(
                    tactical_plan_id=tactical_id, entry_id=entry_id,
                    ticker=ticker, result_json=ep,
                    reason="; ".join(vres.violations),
                    marker=store.BLOCK_MARKER_SYSTEM,
                )
                blocked += 1
                logger.info("拦截不合规执行计划 %s: %s", ticker, vres.violations)
                continue

            plan_id = store.create_execution_plan(
                tactical_plan_id=tactical_id,
                entry_id=entry_id,
                ticker=ticker,
                result_json=ep,
            )
            created_ids.append(plan_id)

        for cid in applied_card_ids:
            store.increment_card_applied(cid)

        extra = []
        if skipped:
            extra.append(f"跳过 {skipped} 条重复")
        if repaired:
            extra.append(f"自修正 {repaired} 条")
        if blocked:
            extra.append(f"拦截 {blocked} 条不合规")
        extra_msg = ("，" + "，".join(extra)) if extra else ""
        yield _sse("decision_done", layer="L4",
                   plan_count=len(created_ids),
                   blocked_count=blocked,
                   repaired_count=repaired,
                   execution_summary=result.get("execution_summary", {}),
                   skipped=result.get("skipped_plans", []),
                   message=f"L4 执行方案已生成：{len(created_ids)} 条待确认操作{extra_msg}")

        # P3.2 过度交易监控：近 7 天成交超阈值则告警
        try:
            from datetime import timedelta as _td
            recent = store.get_sim_trades(limit=100)
            cutoff = (datetime.now(timezone.utc) - _td(days=7)).isoformat()
            recent_count = sum(1 for t in recent if (t.get("created_at", "") or "") >= cutoff)
            if recent_count >= 15:
                yield _sse("decision_warning", layer="L4",
                           message=f"⚠ 过度交易提示：近7天已成交 {recent_count} 笔，注意手续费与择时损耗")
        except Exception:
            pass

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
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """完整日常决策流程：L1→L2→L3→L4→投委会

    scope: "l1" = 仅 L1 检查
           "l3l4" = 仅 L3-L4 更新
           "full" = 全流程
    """
    store = store.for_market(market)
    yield _sse("daily_start", scope=scope, market=market, message="开始日常决策流程...")

    # Step 0: 催化剂时效检查
    try:
        from bottleneck_hunter.watchlist.catalyst_monitor import check_catalyst_expiry
        async for evt in check_catalyst_expiry(store):
            yield evt
    except Exception as e:
        logger.warning("催化剂时效检查失败: %s", e)

    # Step 0.5: 投资论点有效性检查
    try:
        from bottleneck_hunter.watchlist.thesis_tracker import check_all_theses
        async for evt in check_all_theses(store):
            yield evt
    except Exception as e:
        logger.warning("论点检查失败: %s", e)

    # Step 1: L1 宏观检查
    if scope in ("l1", "full"):
        async for evt in run_macro_check(store, budget, market=market):
            yield evt

    # Step 2: L2 偏离检查 + pre_l2 质量门控
    if scope in ("full",):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks
            async for evt in run_quality_checks(store, "pre_l2"):
                yield evt

            macro = store.get_latest_macro_strategy()
            plan = store.get_latest_strategic_plan()

            if not plan and macro:
                yield _sse("decision_info", layer="L2",
                           message="无 L2 组合策略，自动生成...")
                async for evt in run_strategic_plan(store, budget, market=market):
                    yield evt
            elif plan:
                async for evt in run_deviation_check(store, budget, market=market):
                    yield evt
        except Exception as e:
            logger.exception("L2 阶段失败")
            yield _sse("decision_error", layer="L2", error=str(e))

    if scope == "l1":
        yield _sse("daily_done", message="L1 检查完成")
        return

    # Step 2.5: pre_l3 质量门控
    if scope in ("l3l4", "full"):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks
            async for evt in run_quality_checks(store, "pre_l3"):
                yield evt
        except Exception as e:
            logger.warning("pre_l3 质量门控失败: %s", e)

    # Step 3: L3 战术计划
    if scope in ("l3l4", "full"):
        async for evt in run_tactical_plans(store, budget, market=market):
            yield evt

    # Step 3.5: pre_l4 质量门控
    if scope in ("l3l4", "full"):
        try:
            from bottleneck_hunter.watchlist.quality_gate import run_quality_checks
            async for evt in run_quality_checks(store, "pre_l4"):
                yield evt
        except Exception as e:
            logger.warning("pre_l4 质量门控失败: %s", e)

    # Step 4: L4 执行方案
    if scope in ("l3l4", "full"):
        async for evt in run_execution_plans(store, budget, market=market):
            yield evt

    # Step 5: 投委会评审
    if scope in ("l3l4", "full"):
        try:
            pending = store.get_pending_executions()
            if pending:
                from bottleneck_hunter.watchlist.committee import run_committee_review
                yield _sse("decision_info", layer="committee",
                           message=f"启动投委会评审 {len(pending)} 条执行计划...")
                async for evt in run_committee_review(store, pending, budget, market=market):
                    yield evt
            else:
                yield _sse("decision_info", layer="committee",
                           message="无待评审执行计划，跳过投委会")
        except Exception as e:
            logger.exception("投委会评审失败")
            yield _sse("decision_error", layer="committee", error=str(e))

    # Step 6: 更新观察池综合评分（裸调用需保护，否则崩溃会中断 SSE 流导致前端面板空白）
    try:
        _update_composite_scores(store, market)
    except Exception as e:
        logger.exception("综合评分更新失败")
        yield _sse("decision_error", layer="composite", error=str(e))

    yield _sse("daily_done", message="日常决策流程完成")


async def run_full_refresh(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """全量刷新：重新生成 L1 + L2 + L3 + L4 + 投委会"""
    store = store.for_market(market)
    yield _sse("refresh_start", message="开始全量决策刷新...")

    async for evt in run_macro_strategy(store, budget, market=market):
        yield evt

    async for evt in run_strategic_plan(store, budget, market=market):
        yield evt

    async for evt in run_tactical_plans(store, budget, market=market):
        yield evt

    async for evt in run_execution_plans(store, budget, market=market):
        yield evt

    pending = store.get_pending_executions()
    if pending:
        from bottleneck_hunter.watchlist.committee import run_committee_review
        async for evt in run_committee_review(store, pending, budget, market=market):
            yield evt

    try:
        _update_composite_scores(store, market)
    except Exception as e:
        logger.exception("综合评分更新失败")
        yield _sse("decision_error", layer="composite", error=str(e))
    yield _sse("refresh_done", message="全量决策刷新完成")


# ─────────────────────────────────────────────────────────
# 数据收集辅助
# ─────────────────────────────────────────────────────────

async def _collect_market_context(store: WatchlistStore, market: str = "us_stock") -> dict:
    """收集市场宏观数据（从观察池数据中聚合），并附带市场类型列表。"""
    from bottleneck_hunter.watchlist.macro_data import fetch_macro_data

    by_market = store.get_tickers_by_market()
    tickers = by_market.get(market, [])
    active_markets = [market]

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

    entries = [e for e in store.list_all() if e.get("market") == market]
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

    try:
        macro = await fetch_macro_data(store, active_markets)
    except Exception as e:
        logger.warning("宏观数据采集失败，使用缓存: %s", e)
        macro = {}
        cached = store.get_latest_macro_snapshots()
        for row in cached:
            macro[row["indicator"]] = {"value": row["value"], "change_pct": 0.0, "label": row["indicator"]}

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
        "macro": macro,
        "news": news_items[:10],
        "markets": active_markets,
    }


def _collect_watchlist_signals(store: WatchlistStore, market: str = "us_stock") -> list[dict]:
    """从已有的 strategy_records 收集个股信号"""
    entries = store.list_all()
    entries = [e for e in entries if e.get("market") == market]
    signals = []

    strategy_summaries = store.get_all_strategy_summaries()

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]
        summary = strategy_summaries.get(entry_id, {})

        snap = store.get_latest_snapshot(ticker)

        if snap and snap.get("data_quality") == "suspended":
            logger.info("跳过停牌股 %s", ticker)
            continue

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


def _update_composite_scores(store: WatchlistStore, market: str = "us_stock") -> None:
    """根据策略信心、投委会评分、催化剂活跃度计算并更新观察池综合评分。"""
    entries = store.list_all()
    entries = [e for e in entries if e.get("market") == market]
    strategy_summaries = store.get_all_strategy_summaries()

    # P3.3 绩效驱动的动态层权重(样本不足时回退默认 0.4/0.3)
    w_review, w_conf = _layer_weight_factors(store)

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]
        try:
            strategy = strategy_summaries.get(entry_id, {})
            confidence = strategy.get("confidence", 5)

            reviews = _get_latest_reviews_for_ticker(store, ticker)
            if reviews:
                avg_score = sum(r.get("score", 5) or 5 for r in reviews) / len(reviews)
            else:
                avg_score = 5.0

            catalysts = store.get_catalysts_for_ticker(ticker)
            active_catalysts = [c for c in catalysts if c.get("status") in ("pending", "monitoring")]
            catalyst_score = min(len(active_catalysts) * 3, 10)

            snap = store.get_latest_snapshot(ticker)
            if snap and snap.get("fetched_at"):
                from datetime import datetime, timezone
                try:
                    fetched = datetime.fromisoformat(snap["fetched_at"].replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
                    freshness = max(0, min(10, 10 - age_hours / 12))
                except (ValueError, TypeError):
                    freshness = 5.0
            else:
                freshness = 0.0

            composite = round(
                avg_score * w_review +
                confidence * w_conf +
                catalyst_score * 0.15 +
                freshness * 0.15,
                2,
            )

            store.update(entry_id, composite_score=composite)
        except Exception as e:
            # 单标的失败不阻断其余标的评分更新
            logger.warning("综合评分更新失败 %s: %s", ticker, e)

    logger.info("更新了 %d 个标的的综合评分", len(entries))


def _layer_weight_factors(store: WatchlistStore) -> tuple[float, float]:
    """P3.3 绩效驱动：基于 layer_performance 历史表现，返回(委评权重, 信心权重)。

    L2(选股)历史准→委评层加权；L3(择时)历史准→信心层加权。
    仅在样本≥5时启用，调整幅度限制在基准 ±30% 内，且两者之和恒为 0.7。
    """
    base_review, base_conf = 0.4, 0.3
    try:
        summary = store.get_layer_performance_summary()
    except Exception:
        return base_review, base_conf
    l2 = summary.get("L2", {})
    l3 = summary.get("L3", {})
    if l2.get("count", 0) < 5 or l3.get("count", 0) < 5:
        return base_review, base_conf
    # 以 5 分为中性基准，>5 加权 <5 减权，归一化到总和 0.7
    l2_avg = l2.get("avg", 5)
    l3_avg = l3.get("avg", 5)
    # 限制偏移 ±30%
    l2_factor = max(0.7, min(1.3, l2_avg / 5))
    l3_factor = max(0.7, min(1.3, l3_avg / 5))
    raw_review = base_review * l2_factor
    raw_conf = base_conf * l3_factor
    total = raw_review + raw_conf
    if total <= 0:
        return base_review, base_conf
    # 归一化到原总和 0.7
    scale = 0.7 / total
    return round(raw_review * scale, 3), round(raw_conf * scale, 3)


def _get_latest_reviews_for_ticker(store: WatchlistStore, ticker: str) -> list[dict]:
    """获取某 ticker 最近一批投委会评审。"""
    conn = store._connect()
    try:
        q, p = store._user_filter(
            """SELECT cr.score FROM committee_reviews cr
               JOIN execution_plans ep ON cr.execution_plan_id = ep.id
               WHERE ep.ticker = ?
               ORDER BY cr.created_at DESC LIMIT 4""",
            (ticker,),
            table="cr",
        )
        rows = conn.execute(q, p).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
