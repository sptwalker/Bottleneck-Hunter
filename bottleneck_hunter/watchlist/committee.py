"""投资委员会 — 多 LLM 并行评审 + 圆桌讨论 + 共识表决

4 位委员使用不同 LLM 提供商，独立评审 L4 执行计划：
- 风险控制官 (risk)     → deepseek
- 成长投资人 (growth)   → qwen
- 价值投资人 (value)    → kimi
- 逆向投资人 (contrarian) → glm

分歧超阈值时自动触发圆桌讨论，最终汇总共识。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.llm_clients.factory import get_llm_for_position

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"

MEMBERS = [
    {
        "role": "risk_officer",
        "label": "🛡 风险控制官",
        "prompt_file": "committee_risk",
        "provider_hint": "deepseek",
        "config_key": "committee_risk",
    },
    {
        "role": "growth_investor",
        "label": "📈 成长投资人",
        "prompt_file": "committee_growth",
        "provider_hint": "qwen",
        "config_key": "committee_growth",
    },
    {
        "role": "value_investor",
        "label": "💎 价值投资人",
        "prompt_file": "committee_value",
        "provider_hint": "kimi",
        "config_key": "committee_value",
    },
    {
        "role": "contrarian",
        "label": "🔄 逆向投资人",
        "prompt_file": "committee_contrarian",
        "provider_hint": "glm",
        "config_key": "committee_contrarian",
    },
]


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt 模板不存在: {path}")


# ─────────────────────────────────────────────────────────
# 单成员评审
# ─────────────────────────────────────────────────────────

async def _review_single(
    member: dict,
    execution_plan: dict,
    context: dict,
) -> dict:
    """单个委员独立评审"""
    llm, provider, model = get_llm_for_position(position=member.get("config_key"), provider_hint=member["provider_hint"])
    if not llm:
        llm, provider, model = get_llm_for_position()
    if not llm:
        return {"role": member["role"], "error": "无可用 LLM", "vote": "abstain"}

    prompt_template = _load_prompt(member["prompt_file"])

    plan_json = json.dumps(execution_plan, ensure_ascii=False)
    macro_summary = context.get("macro_summary", "暂无宏观环境数据")
    account_status = json.dumps(context.get("account_status", {}), ensure_ascii=False)

    format_vars = {
        "execution_plan": plan_json,
        "account_status": account_status,
        "macro_summary": macro_summary,
        "market_context": context.get("market_context", ""),
        "catalyst_data": json.dumps(context.get("catalyst_data", []), ensure_ascii=False),
        "sector_trends": context.get("sector_trends", "暂无行业趋势数据"),
        "valuation_data": json.dumps(context.get("valuation_data", {}), ensure_ascii=False),
        "peer_comparison": context.get("peer_comparison", "暂无同行业对比数据"),
        "sentiment_data": context.get("sentiment_data", "暂无市场情绪数据"),
        "crowding_data": context.get("crowding_data", "暂无持仓集中度数据"),
    }

    prompt = prompt_template
    for k, v in format_vars.items():
        prompt = prompt.replace("{" + k + "}", v)

    try:
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        result = extract_json_object(response)
        result["role"] = member["role"]
        result["provider"] = provider
        result["model"] = model
        return result
    except Exception as e:
        logger.warning("委员 %s 评审失败: %s", member["role"], e)
        return {"role": member["role"], "error": str(e), "vote": "abstain",
                "provider": provider, "model": model}


# ─────────────────────────────────────────────────────────
# 圆桌讨论
# ─────────────────────────────────────────────────────────

async def _run_discussion(
    disputed_ticker: str,
    reviews: dict[str, dict],
    execution_plan: dict,
) -> dict:
    """当委员分歧过大时，触发圆桌讨论"""
    llm, provider, model = get_llm_for_position(position="committee_consensus", provider_hint="deepseek")
    if not llm:
        llm, provider, model = get_llm_for_position()
    if not llm:
        return {"error": "无可用 LLM 进行圆桌讨论"}

    prompt_template = _load_prompt("committee_discussion")
    prompt = (prompt_template
              .replace("{disputed_ticker}", disputed_ticker)
              .replace("{risk_officer_review}", json.dumps(reviews.get("risk_officer", {}), ensure_ascii=False))
              .replace("{growth_investor_review}", json.dumps(reviews.get("growth_investor", {}), ensure_ascii=False))
              .replace("{value_investor_review}", json.dumps(reviews.get("value_investor", {}), ensure_ascii=False))
              .replace("{contrarian_review}", json.dumps(reviews.get("contrarian", {}), ensure_ascii=False))
              .replace("{original_plan}", json.dumps(execution_plan, ensure_ascii=False))
              )

    response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
    return extract_json_object(response)


# ─────────────────────────────────────────────────────────
# 共识汇总
# ─────────────────────────────────────────────────────────

async def _build_consensus(
    reviews: dict[str, dict],
    discussion_results: dict | None = None,
) -> dict:
    """汇总评审意见，生成最终共识"""
    llm, provider, model = get_llm_for_position(position="committee_consensus", provider_hint="deepseek")
    if not llm:
        llm, provider, model = get_llm_for_position()
    if not llm:
        return _fallback_consensus(reviews)

    prompt_template = _load_prompt("committee_consensus")
    prompt = (prompt_template
              .replace("{risk_review}", json.dumps(reviews.get("risk_officer", {}), ensure_ascii=False))
              .replace("{growth_review}", json.dumps(reviews.get("growth_investor", {}), ensure_ascii=False))
              .replace("{value_review}", json.dumps(reviews.get("value_investor", {}), ensure_ascii=False))
              .replace("{contrarian_review}", json.dumps(reviews.get("contrarian", {}), ensure_ascii=False))
              .replace("{discussion_results}",
                       json.dumps(discussion_results, ensure_ascii=False) if discussion_results else "无圆桌讨论")
              )

    try:
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        return extract_json_object(response)
    except Exception as e:
        logger.warning("共识汇总 LLM 失败，使用规则引擎: %s", e)
        return _fallback_consensus(reviews)


def _fallback_consensus(reviews: dict[str, dict]) -> dict:
    """规则引擎兜底共识"""
    votes = {}
    for role, review in reviews.items():
        vote = review.get("vote", "abstain")
        votes[role] = {"vote": vote, "confidence": review.get("confidence", 5)}

    approve_count = sum(1 for v in votes.values()
                        if v["vote"] in ("approve", "approve_with_modification"))
    reject_count = sum(1 for v in votes.values() if v["vote"] == "reject")
    total = len(votes) or 1

    if approve_count >= 3:
        verdict = "approved"
    elif reject_count >= 3:
        verdict = "rejected"
    elif approve_count == 2 and reject_count == 2:
        verdict = "needs_discussion"
    elif approve_count >= 2:
        verdict = "approved_with_modifications"
    else:
        verdict = "rejected"

    return {
        "final_verdict": verdict,
        "approval_rate": round(approve_count / total * 100),
        "vote_detail": votes,
        "consensus_modifications": [],
        "final_execution_plan": [],
        "key_risks_flagged": [],
        "minority_opinions": [],
        "summary": f"投票结果: {approve_count} 票赞成, {reject_count} 票反对（规则引擎兜底）",
    }


def _needs_discussion(reviews: dict[str, dict]) -> bool:
    """判断是否需要圆桌讨论"""
    votes = [r.get("vote", "abstain") for r in reviews.values()]
    approve = sum(1 for v in votes if v in ("approve", "approve_with_modification"))
    reject = sum(1 for v in votes if v == "reject")
    if approve == 2 and reject == 2:
        return True
    confidences = [r.get("confidence", 5) for r in reviews.values()]
    if confidences and max(confidences) - min(confidences) >= 5:
        return True
    return False


# ─────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────

async def run_committee_review(
    store: WatchlistStore,
    pending_plans: list[dict],
    budget: BudgetTracker | None = None,
    market: str = "us_stock",
) -> AsyncGenerator[dict, None]:
    """对待审执行计划逐一进行投委会评审"""
    store = store.for_market(market)
    total = len(pending_plans)
    yield _sse("committee_start", total=total,
               message=f"投委会评审启动，共 {total} 条执行计划")

    macro = store.get_latest_macro_strategy()
    account = store.get_sim_account()
    positions = store.get_sim_positions(account.get("id"))

    from bottleneck_hunter.watchlist.decision_engine import _get_market_context_text
    active_markets = list(store.get_tickers_by_market().keys())
    market_ctx = _get_market_context_text(active_markets)

    context = {
        "market_context": market_ctx,
        "macro_summary": (macro.get("market_summary", "") if macro
                          else "暂无宏观环境数据"),
        "account_status": {
            "total_equity": account.get("total_equity", 100000),
            "cash_balance": account.get("cash_balance", 100000),
            "positions": [{"ticker": p["ticker"], "shares": p.get("shares", 0),
                           "avg_cost": p.get("avg_cost", 0),
                           "market_value": p.get("market_value", 0)}
                          for p in positions],
        },
        "catalyst_data": [],
        "sector_trends": "暂无行业趋势数据",
        "valuation_data": {},
        "peer_comparison": "暂无同行业对比数据",
        "sentiment_data": "暂无市场情绪数据",
        "crowding_data": "暂无持仓集中度数据",
    }

    for idx, plan in enumerate(pending_plans, 1):
        plan_id = plan.get("id", "")
        ticker = plan.get("ticker", "unknown")
        exec_plan = plan.get("result_json", plan)

        yield _sse("committee_plan_start", index=idx, total=total,
                   ticker=ticker, plan_id=plan_id,
                   message=f"评审 [{idx}/{total}] {ticker}...")

        entry_id = plan.get("entry_id", "")
        if entry_id:
            catalysts = store.get_catalysts_for_entry(entry_id, active_only=True)
            context["catalyst_data"] = catalysts

        if budget and not budget.can_spend(estimated_tokens=15000):
            yield _sse("committee_error", ticker=ticker, error="预算不足，跳过后续评审")
            break

        # 4 位委员并行评审
        tasks = [_review_single(m, exec_plan, context) for m in MEMBERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        reviews: dict[str, dict] = {}
        for r in results:
            if isinstance(r, Exception):
                logger.warning("委员评审异常: %s", r)
                continue
            role = r.get("role", "unknown")
            reviews[role] = r
            store.create_committee_review(
                execution_plan_id=plan_id,
                member_role=role,
                model_provider=r.get("provider", ""),
                model_name=r.get("model", ""),
                result_json=r,
            )
            try:
                store.record_prediction(
                    provider=r.get("provider", ""),
                    model=r.get("model", ""),
                    role_context=f"committee_{role}",
                    ticker=ticker,
                    prediction_type="vote",
                    prediction_value=r.get("vote", "abstain"),
                    market=market,
                )
            except Exception:
                logger.debug("record_prediction failed for committee %s", role)

        yield _sse("committee_reviews_done", ticker=ticker,
                   votes={role: r.get("vote", "abstain") for role, r in reviews.items()},
                   message=f"{ticker} 独立评审完成")

        # 判断是否需要圆桌讨论
        discussion_result = None
        if _needs_discussion(reviews):
            yield _sse("committee_discussion_start", ticker=ticker,
                       message=f"{ticker} 意见分歧，启动圆桌讨论...")
            try:
                discussion_result = await _run_discussion(ticker, reviews, exec_plan)
                yield _sse("committee_discussion_done", ticker=ticker,
                           consensus_reached=discussion_result.get("consensus_reached", False),
                           message=f"{ticker} 圆桌讨论完成")
            except Exception as e:
                logger.warning("圆桌讨论失败: %s", e)
                yield _sse("committee_discussion_error", ticker=ticker, error=str(e))

        # 生成共识
        try:
            consensus = await _build_consensus(reviews, discussion_result)
        except Exception as e:
            logger.warning("共识生成失败: %s", e)
            consensus = _fallback_consensus(reviews)

        store.create_committee_consensus(
            execution_plan_id=plan_id,
            result_json=consensus,
        )

        # ── P0.5 投委会 gating：按共识结论实际动作 ──
        verdict_raw = consensus.get("final_verdict", "unknown")
        summary_text = consensus.get("summary", "")
        if verdict_raw == "rejected":
            store.reject_execution(
                plan_id, f"{store.BLOCK_MARKER_COMMITTEE} {summary_text}")
            yield _sse("committee_gating", ticker=ticker, plan_id=plan_id,
                       action="blocked",
                       message=f"{ticker} 被投委会否决，已移出待确认队列")
        elif verdict_raw == "approved_with_modifications":
            mods: dict = {}
            for m in consensus.get("consensus_modifications", []):
                m_ticker = m.get("ticker")
                if m_ticker and m_ticker != ticker:
                    continue
                field = m.get("field", "")
                val = m.get("modified")
                if field in ("shares", "target_price", "limit_price",
                             "execution_method", "method") and val is not None:
                    mods[field] = val
            if mods:
                ok = store.apply_committee_modifications(plan_id, mods)
                if ok:
                    yield _sse("committee_gating", ticker=ticker, plan_id=plan_id,
                               action="modified", modifications=mods,
                               message=f"{ticker} 已应用投委会修改: {mods}")

        try:
            participants = [
                {"role": r.get("role", ""), "name": r.get("name", ""),
                 "model": f"{r.get('provider', '')}/{r.get('model', '')}"}
                for r in reviews.values()
            ]
            store.create_meeting_record(
                meeting_type="committee",
                title=f"投委会审议: {ticker} {plan.get('action', '')}",
                participants=participants,
                tickers_discussed=[ticker],
                final_verdict=consensus.get("final_verdict", ""),
                key_agreements=consensus.get("key_agreements", []),
                key_disagreements=consensus.get("minority_opinions", []),
                risk_warnings=consensus.get("key_risks_flagged", []),
                result_json=consensus,
                execution_plan_id=plan_id,
                market=market,
            )
        except Exception:
            logger.debug("create_meeting_record failed for committee %s", ticker)

        verdict = consensus.get("final_verdict", "unknown")
        yield _sse("committee_plan_done", ticker=ticker, plan_id=plan_id,
                   verdict=verdict,
                   approval_rate=consensus.get("approval_rate", 0),
                   summary=consensus.get("summary", ""),
                   message=f"{ticker} 评审结果: {verdict}")

        if budget:
            budget.record("committee", "multi", 15000, 6000, f"committee_{ticker}")

    yield _sse("committee_done", total=total,
               message=f"投委会评审完成，共处理 {total} 条执行计划")
