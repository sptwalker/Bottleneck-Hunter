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

# 降级备用 provider 优先级（与主模型不同的依次尝试）
_FALLBACK_PROVIDERS = ["deepseek", "qwen", "glm", "kimi"]


def _build_llm_chain(member: dict) -> list[tuple]:
    """构建该委员的 LLM 调用链：主模型 + 一个不同 provider 的备用模型。

    返回 [(llm, provider, model), ...]，供失败降级依次尝试。
    """
    chain: list[tuple] = []
    seen: set[str] = set()
    llm, provider, model = get_llm_for_position(
        position=member.get("config_key"), provider_hint=member["provider_hint"])
    if llm:
        chain.append((llm, provider, model))
        seen.add(provider)
    # 备用：选一个与主模型不同且可用的 provider
    for hint in _FALLBACK_PROVIDERS:
        if hint in seen:
            continue
        fl, fp, fm = get_llm_for_position(provider_hint=hint)
        if fl and fp not in seen:
            chain.append((fl, fp, fm))
            seen.add(fp)
            break
    # 仍为空则退到通用默认
    if not chain:
        dl, dp, dm = get_llm_for_position()
        if dl:
            chain.append((dl, dp, dm))
    return chain


_TRANSIENT_KEYS = ("429", "overload", "rate limit", "ratelimit", "timeout",
                   "timed out", "503", "502", "500", "busy", "unavailable",
                   "temporarily")


async def _invoke_with_retry(chain: list[tuple], prompt: str, role: str,
                             max_retry: int = 2) -> tuple[str, str, str]:
    """带重试 + 降级的 LLM 调用。

    对每个模型重试 max_retry 次（仅瞬态错误退避重试），失败则切换到链中下一个备用模型。
    返回 (content, provider, model)；全部失败则抛出最后一个异常。
    """
    last_err: Exception | None = None
    for idx, (llm, provider, model) in enumerate(chain):
        for attempt in range(max_retry):
            try:
                content = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
                if idx > 0 or attempt > 0:
                    logger.info("委员 %s 经重试/降级成功（%s/%s, 第%d次）",
                                role, provider, model, attempt + 1)
                return content, provider, model
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                transient = any(k in msg for k in _TRANSIENT_KEYS)
                logger.warning("委员 %s 调用 %s/%s 失败(%s): %s",
                               role, provider, model,
                               "瞬态" if transient else "非瞬态", e)
                if not transient:
                    break  # 非瞬态错误：不在同模型重试，直接换备用模型
                if attempt < max_retry - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))  # 退避
    raise last_err or RuntimeError(f"委员 {role} 无可用 LLM")


async def _review_single(
    member: dict,
    execution_plan: dict,
    context: dict,
) -> dict:
    """单个委员独立评审（第 1 轮）"""
    chain = _build_llm_chain(member)
    if not chain:
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
        "catalyst_data": context.get("catalyst_data", []),
        "sector_trends": context.get("sector_trends", "暂无行业趋势数据"),
        "valuation_data": context.get("valuation_data", {}),
        "peer_comparison": context.get("peer_comparison", "暂无同行业对比数据"),
        "sentiment_data": context.get("sentiment_data", "暂无市场情绪数据"),
        "crowding_data": context.get("crowding_data", "暂无持仓集中度数据"),
    }

    prompt = prompt_template
    for k, v in format_vars.items():
        # 背景资料可能是 dict/list（build_ticker_background 的真实数据），统一序列化为字符串
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, default=str)
        prompt = prompt.replace("{" + k + "}", v)

    provider, model = "", ""
    try:
        response, provider, model = await _invoke_with_retry(chain, prompt, member["role"])
        result = extract_json_object(response)
        result["role"] = member["role"]
        result["provider"] = provider
        result["model"] = model
        return result
    except Exception as e:
        logger.warning("委员 %s 评审失败(已重试+降级): %s", member["role"], e)
        return {"role": member["role"], "error": str(e), "vote": "abstain",
                "provider": provider, "model": model}


# ─────────────────────────────────────────────────────────
# 第 2 轮：辩论与质疑
# ─────────────────────────────────────────────────────────

def _summarize_round1(reviews: dict[str, dict], exclude_role: str = "") -> str:
    """把第 1 轮各委员评审压缩为简报（供第 2 轮互相质疑）。"""
    role_label = {m["role"]: m["label"] for m in MEMBERS}
    parts = []
    for role, r in reviews.items():
        if role == exclude_role:
            continue
        if r.get("error"):
            continue  # 跳过失败的委员
        concerns = r.get("key_concerns", [])
        concern_str = "；".join(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                                for c in concerns[:3])
        parts.append(
            f"### {role_label.get(role, role)}\n"
            f"- 投票：{r.get('vote', 'abstain')}（信心 {r.get('confidence', 5)}/10）\n"
            f"- 观点：{r.get('overall_assessment', '')}\n"
            f"- 关注点：{concern_str or '无'}"
        )
    return "\n\n".join(parts) if parts else "（其他委员无有效评审）"


async def _review_round2(
    member: dict,
    execution_plan: dict,
    round1_reviews: dict[str, dict],
) -> dict:
    """第 2 轮：委员看到其他人第 1 轮意见后，重新评估并给出终票。"""
    role = member["role"]
    my_r1 = round1_reviews.get(role, {})
    # 若该委员第 1 轮就失败，第 2 轮不再尝试，沿用其失败态
    if my_r1.get("error"):
        return my_r1

    chain = _build_llm_chain(member)
    if not chain:
        return my_r1  # 无可用 LLM，沿用第 1 轮

    template = _load_prompt("committee_rebuttal")
    prompt = (template
              .replace("{member_label}", member["label"])
              .replace("{execution_plan}", json.dumps(execution_plan, ensure_ascii=False))
              .replace("{my_round1}", json.dumps({
                  "vote": my_r1.get("vote"), "confidence": my_r1.get("confidence"),
                  "overall_assessment": my_r1.get("overall_assessment", ""),
                  "key_concerns": my_r1.get("key_concerns", []),
              }, ensure_ascii=False))
              .replace("{peers_round1}", _summarize_round1(round1_reviews, exclude_role=role)))

    try:
        response, provider, model = await _invoke_with_retry(chain, prompt, role)
        result = extract_json_object(response)
        result["role"] = role
        result["provider"] = provider
        result["model"] = model
        # 第 2 轮统一字段：rebuttal 作为发言内容
        if result.get("rebuttal") and not result.get("overall_assessment"):
            result["overall_assessment"] = result["rebuttal"]
        return result
    except Exception as e:
        logger.warning("委员 %s 第2轮辩论失败，沿用第1轮: %s", role, e)
        return my_r1  # 第 2 轮失败则保留第 1 轮票


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
# 背景资料补全（阶段 1.1）：把占位桩接真实数据
# ─────────────────────────────────────────────────────────

def _fmt_num(v, nd=2):
    try:
        if v is None or v == "":
            return None
        return round(float(v), nd)
    except (ValueError, TypeError):
        return None


def build_ticker_background(store: WatchlistStore, ticker: str, entry_id: str,
                            market: str) -> dict:
    """为单只标的聚合投委会所需的真实背景资料。

    返回 dict，键对应各委员 prompt 占位符：
      catalyst_data / valuation_data / sentiment_data / crowding_data
      / peer_comparison / sector_trends
    每项取数失败时降级为简短"暂无"文本，不阻断评审。
    """
    bg: dict = {}

    # 催化剂（成长投资人）
    try:
        bg["catalyst_data"] = (store.get_catalysts_for_entry(entry_id, active_only=True)
                               if entry_id else [])
    except Exception:
        bg["catalyst_data"] = []

    # 估值（价值投资人）← company_profile.raw + 最新快照
    try:
        prof = store.get_company_profile(ticker) or {}
        raw = prof.get("raw", {}) if isinstance(prof.get("raw"), dict) else {}
        snap = store.get_latest_snapshot(ticker) or {}
        val = {
            "trailing_pe": _fmt_num(raw.get("trailingPE")),
            "forward_pe": _fmt_num(raw.get("forwardPE")),
            "price_to_book": _fmt_num(raw.get("priceToBook")),
            "price_to_sales": _fmt_num(raw.get("priceToSalesTrailing12Months")),
            "ev_to_ebitda": _fmt_num(raw.get("enterpriseToEbitda")),
            "peg": _fmt_num(raw.get("pegRatio") or raw.get("trailingPegRatio")),
            "profit_margin": _fmt_num(raw.get("profitMargins")),
            "roe": _fmt_num(raw.get("returnOnEquity")),
            "revenue_growth": _fmt_num(raw.get("revenueGrowth")),
            "current_price": _fmt_num(snap.get("close")),
            "market_cap": snap.get("market_cap") or raw.get("marketCap"),
            "sector": prof.get("sector", "") or raw.get("sector", ""),
        }
        bg["valuation_data"] = ({k: v for k, v in val.items() if v is not None}
                                or "暂无估值数据（未采集 profile）")
    except Exception:
        bg["valuation_data"] = "暂无估值数据"

    # 情绪（逆向投资人）← 新闻情感 + 期权 PCR
    try:
        news = store.get_news(ticker, limit=15) or []
        scores = [n.get("sentiment_score") for n in news
                  if isinstance(n.get("sentiment_score"), (int, float))]
        avg_sent = round(sum(scores) / len(scores), 3) if scores else None
        pos = sum(1 for n in news if n.get("sentiment") == "positive")
        neg = sum(1 for n in news if n.get("sentiment") == "negative")
        opts = store.get_options(ticker, limit=1) or []
        pcr = _fmt_num(opts[0].get("put_call_ratio")) if opts else None
        sent = {
            "news_count": len(news),
            "avg_sentiment_score": avg_sent,
            "positive_news": pos,
            "negative_news": neg,
            "put_call_ratio": pcr,
            "recent_headlines": [n.get("title", "") for n in news[:5] if n.get("title")],
        }
        # 只过滤真正缺失（None/空列表），保留合法 0 值（如中性情绪、零正面新闻），
        # 否则"零正面新闻"与"未采集"无法区分
        bg["sentiment_data"] = ({k: v for k, v in sent.items()
                                 if v not in (None, [])} or "暂无市场情绪数据")
    except Exception:
        bg["sentiment_data"] = "暂无市场情绪数据"

    # 拥挤度（逆向投资人）← 机构持仓 + 分析师评级分布 + 内部人交易
    try:
        holders = store.get_institutional_holders(ticker, limit=10) or []
        ratings = store.get_analyst_ratings(ticker, limit=30) or []
        rating_dist: dict = {}
        for r in ratings:
            key = (r.get("rating", "") or "未知").lower()
            rating_dist[key] = rating_dist.get(key, 0) + 1
        insiders = store.get_insider_trades(ticker, limit=10) or []
        insider_buy = sum(1 for t in insiders
                          if "buy" in (t.get("transaction_type", "") or "").lower()
                          or "购" in (t.get("transaction_type", "") or ""))
        insider_sell = len(insiders) - insider_buy
        crowd = {
            "top_institutional_holders": [
                {"name": h.get("holder_name", ""), "pct": _fmt_num(h.get("pct_held"))}
                for h in holders[:5]
            ],
            "analyst_rating_distribution": rating_dist or "无评级数据",
            "analyst_count": len(ratings),
            "insider_buy_count": insider_buy,
            "insider_sell_count": insider_sell,
        }
        bg["crowding_data"] = crowd
    except Exception:
        bg["crowding_data"] = "暂无持仓集中度数据"

    # 同业对比（价值投资人）← 同 sector 观察池标的估值轻量聚合
    try:
        sector = ""
        prof = store.get_company_profile(ticker) or {}
        sector = prof.get("sector", "")
        peers = []
        if sector:
            for e in store.list_all():
                tk = e.get("ticker", "")
                if not tk or tk == ticker or e.get("sector", "") != sector:
                    continue
                psnap = store.get_latest_snapshot(tk) or {}
                pprof = store.get_company_profile(tk) or {}
                praw = pprof.get("raw", {}) if isinstance(pprof.get("raw"), dict) else {}
                peers.append({
                    "ticker": tk,
                    "pe": _fmt_num(praw.get("trailingPE")),
                    "pb": _fmt_num(praw.get("priceToBook")),
                    "change_pct": _fmt_num(psnap.get("change_pct")),
                })
                if len(peers) >= 6:
                    break
        bg["peer_comparison"] = ({"sector": sector, "peers": peers}
                                 if peers else "暂无同业对比数据")
    except Exception:
        bg["peer_comparison"] = "暂无同业对比数据"

    # 行业趋势（成长投资人）← 热点板块（best-effort）
    try:
        from bottleneck_hunter.chain.hot_sector import HotSectorDetector  # noqa
        bg["sector_trends"] = "暂无行业趋势数据"  # 占位：热点板块需独立采集，后续接入
    except Exception:
        bg["sector_trends"] = "暂无行业趋势数据"

    return bg


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
        # 阶段 1.1：用真实数据填充该标的的背景资料（估值/情绪/拥挤度/同业/催化剂）
        try:
            bg = build_ticker_background(store, ticker, entry_id, market)
            context.update(bg)
        except Exception as e:
            logger.warning("背景资料聚合失败 %s: %s", ticker, e)

        if budget and not budget.can_spend(estimated_tokens=15000):
            yield _sse("committee_error", ticker=ticker, error="预算不足，跳过后续评审")
            break

        # ── 第 1 轮：4 位委员并行独立评审 ──
        tasks = [_review_single(m, exec_plan, context) for m in MEMBERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        reviews1: dict[str, dict] = {}
        for r in results:
            if isinstance(r, Exception):
                logger.warning("委员评审异常: %s", r)
                continue
            role = r.get("role", "unknown")
            reviews1[role] = r
            try:
                store.create_committee_review(
                    execution_plan_id=plan_id, member_role=role,
                    model_provider=r.get("provider", ""), model_name=r.get("model", ""),
                    result_json=r,
                )
            except Exception as e:
                logger.warning("保存委员评审失败 %s/%s: %s", ticker, role, e)

        yield _sse("committee_round1_done", ticker=ticker,
                   votes={role: r.get("vote", "abstain") for role, r in reviews1.items()},
                   message=f"{ticker} 第 1 轮独立评审完成")

        # ── 第 2 轮：互相质疑，可改票（基于第 1 轮，需 ≥2 位有效委员才有意义）──
        valid1 = {ro: r for ro, r in reviews1.items() if not r.get("error")}
        reviews2: dict[str, dict] = dict(reviews1)
        revised = []
        if len(valid1) >= 2:
            yield _sse("committee_round2_start", ticker=ticker,
                       message=f"{ticker} 第 2 轮辩论与质疑...")
            r2_tasks = [_review_round2(m, exec_plan, reviews1) for m in MEMBERS]
            r2_results = await asyncio.gather(*r2_tasks, return_exceptions=True)
            for r in r2_results:
                if isinstance(r, Exception) or not isinstance(r, dict):
                    continue
                role = r.get("role", "unknown")
                prev = reviews1.get(role, {})
                reviews2[role] = r
                if r.get("vote") != prev.get("vote"):
                    revised.append({"role": role, "from": prev.get("vote"), "to": r.get("vote")})
            yield _sse("committee_round2_done", ticker=ticker,
                       votes={role: r.get("vote", "abstain") for role, r in reviews2.items()},
                       revised=revised,
                       message=f"{ticker} 第 2 轮辩论完成"
                               + (f"，{len(revised)} 位委员改票" if revised else "，无人改票"))

        # 终票以第 2 轮为准
        reviews = reviews2
        # 投票预测记录用终票
        for role, r in reviews.items():
            try:
                store.record_prediction(
                    provider=r.get("provider", ""), model=r.get("model", ""),
                    role_context=f"committee_{role}", ticker=ticker,
                    prediction_type="vote", prediction_value=r.get("vote", "abstain"),
                    market=market,
                )
            except Exception:
                logger.debug("record_prediction failed for committee %s", role)

        yield _sse("committee_reviews_done", ticker=ticker,
                   votes={role: r.get("vote", "abstain") for role, r in reviews.items()},
                   message=f"{ticker} 评审完成（终票）")

        # 判断是否需要圆桌讨论（基于第 2 轮终票）
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

        try:
            store.create_committee_consensus(
                execution_plan_id=plan_id,
                result_json=consensus,
            )
        except Exception as e:
            logger.warning("保存共识失败 %s: %s", ticker, e)

        # ── P0.5 投委会 gating：按共识结论实际动作 ──
        verdict_raw = consensus.get("final_verdict", "unknown")
        summary_text = consensus.get("summary", "")
        try:
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
        except Exception as e:
            logger.warning("投委会 gating 动作失败 %s: %s", ticker, e)

        try:
            role_label = {m["role"]: m["label"] for m in MEMBERS}
            participants = [
                {"role": r.get("role", ""), "name": role_label.get(r.get("role", ""), r.get("name", "")),
                 "model": f"{r.get('provider', '')}/{r.get('model', '')}"}
                for r in reviews.values()
            ]

            # 阶段 1.3：构建完整会议 transcript（背景快照 + 各委员评审 + 圆桌讨论）
            transcript = []
            # 第 0 条：本次会议各委员读入的背景资料（透明化）
            transcript.append({
                "round": 0, "role": "_background", "name": "会议输入资料",
                "data": {
                    "valuation_data": context.get("valuation_data"),
                    "sentiment_data": context.get("sentiment_data"),
                    "crowding_data": context.get("crowding_data"),
                    "peer_comparison": context.get("peer_comparison"),
                    "catalyst_data": context.get("catalyst_data"),
                    "sector_trends": context.get("sector_trends"),
                    "account_status": context.get("account_status"),
                    "macro_summary": context.get("macro_summary"),
                },
            })
            # 第 1 轮：各委员独立评审（真实首轮立场，用 reviews1 而非终票）
            for role, r in reviews1.items():
                transcript.append({
                    "round": 1, "role": role, "name": role_label.get(role, role),
                    "model": f"{r.get('provider', '')}/{r.get('model', '')}",
                    "vote": r.get("vote", "abstain"),
                    "confidence": r.get("confidence", 5),
                    "content": r.get("overall_assessment", "") or "",
                    "key_concerns": r.get("key_concerns", []),
                    "suggestions": r.get("suggestions", []),
                    "strengths": r.get("strengths", []),
                    # 记录 LLM 调用错误，使"因系统错误弃权"可被前端区分于真实弃权
                    "error": r.get("error", ""),
                })
            # 第 2 轮：辩论后改票/终票（仅记录立场或理由确有变化的委员，避免重复）
            for role, r in reviews2.items():
                prev = reviews1.get(role, {})
                if (r.get("vote") == prev.get("vote")
                        and r.get("overall_assessment") == prev.get("overall_assessment")):
                    continue
                transcript.append({
                    "round": 2, "role": role, "name": role_label.get(role, role),
                    "model": f"{r.get('provider', '')}/{r.get('model', '')}",
                    "vote": r.get("vote", "abstain"),
                    "confidence": r.get("confidence", 5),
                    "content": r.get("overall_assessment", "") or "",
                    "key_concerns": r.get("key_concerns", []),
                    "suggestions": r.get("suggestions", []),
                    "strengths": r.get("strengths", []),
                    "prev_vote": prev.get("vote", ""),
                    "error": r.get("error", ""),
                })
            # 圆桌讨论（如有分歧才触发）
            if discussion_result and not discussion_result.get("error"):
                transcript.append({
                    "round": 2, "role": "_discussion", "name": "圆桌讨论",
                    "content": discussion_result.get("reasoning", "")
                    or discussion_result.get("final_recommendation", {}).get("conditions", ""),
                    "consensus_reached": discussion_result.get("consensus_reached", False),
                    "key_agreement": discussion_result.get("key_agreement", ""),
                    "key_disagreement": discussion_result.get("key_disagreement", ""),
                    "minority_view": discussion_result.get("minority_view", {}),
                })

            model_predictions = [
                {"role": role, "name": role_label.get(role, role),
                 "provider": r.get("provider", ""), "model": r.get("model", ""),
                 "vote": r.get("vote", "abstain"), "confidence": r.get("confidence", 5)}
                for role, r in reviews.items()
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
                transcript_json=transcript,
                model_predictions=model_predictions,
                result_json=consensus,
                execution_plan_id=plan_id,
                market=market,
            )
        except Exception:
            logger.exception("create_meeting_record failed for committee %s", ticker)

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
