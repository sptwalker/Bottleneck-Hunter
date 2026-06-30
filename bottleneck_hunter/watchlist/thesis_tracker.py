"""投资论点追踪引擎 — 结构化论点管理 + 支柱证伪检查 + 证据平衡追踪

借鉴 Anthropic financial-services 的 Thesis Tracker 设计模式：
- 可证伪性支柱框架
- 三级信心评估（High/Medium/Low）
- 平衡证据追踪（supporting vs contradicting）
- 定期强制审查
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.llm_clients.factory import get_llm_for_position

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


async def create_thesis_from_strategy(
    store: WatchlistStore,
    entry_id: str,
    strategy: dict,
    budget=None,
) -> str | None:
    """从策略记录中自动提取投资论点和支柱。

    Returns:
        thesis_id 或 None（提取失败时）
    """
    ticker = strategy.get("ticker", "")
    core_logic = strategy.get("core_logic", "")
    bull_bear = strategy.get("bull_bear_analysis", "{}")

    if not core_logic:
        logger.info("跳过论点提取 %s: 无核心逻辑", ticker)
        return None

    existing = store.get_theses_for_entry(entry_id, active_only=True)
    if existing:
        logger.info("跳过论点提取 %s: 已有 %d 个活跃论点", ticker, len(existing))
        return existing[0]["id"]

    llm, provider, model = get_llm_for_position(position="watchlist_thesis", temperature=0.2)
    if not llm:
        return _create_fallback_thesis(store, entry_id, ticker, core_logic)

    if budget and not budget.can_spend(estimated_tokens=1500):
        return _create_fallback_thesis(store, entry_id, ticker, core_logic)

    try:
        prompt_path = PROMPTS_DIR / "thesis_extract.md"
        template = prompt_path.read_text(encoding="utf-8")
        prompt = template.replace("{ticker}", ticker)
        prompt = prompt.replace("{core_logic}", core_logic)
        prompt = prompt.replace("{bull_bear_analysis}", str(bull_bear))

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        if budget:
            budget.record(provider, model, 800, 500, "thesis_extract")

        parsed = extract_json_object(response)
        if not parsed:
            logger.warning("论点提取 JSON 解析失败 %s", ticker)
            return _create_fallback_thesis(store, entry_id, ticker, core_logic)

        pillars = []
        for p in parsed.get("pillars", []):
            pillars.append({
                "text": p.get("text", ""),
                "falsification": p.get("falsification", ""),
                "weight": p.get("weight", 1.0),
            })

        thesis_id = store.create_thesis(
            entry_id=entry_id,
            ticker=ticker,
            title=parsed.get("thesis_title", f"{ticker} 投资论点"),
            summary=parsed.get("thesis_summary", ""),
            conviction=parsed.get("conviction", "medium"),
            time_horizon=parsed.get("time_horizon", "medium_term"),
            pillars=pillars,
        )
        logger.info("创建投资论点 %s: %s (%d 个支柱)",
                     ticker, parsed.get("thesis_title", ""), len(pillars))
        return thesis_id

    except Exception as e:
        logger.warning("论点提取失败 %s: %s", ticker, e)
        return _create_fallback_thesis(store, entry_id, ticker, core_logic)


def _create_fallback_thesis(
    store: WatchlistStore, entry_id: str, ticker: str, core_logic: str,
) -> str:
    return store.create_thesis(
        entry_id=entry_id,
        ticker=ticker,
        title=f"{ticker} 投资论点",
        summary=core_logic[:200] if core_logic else "",
        conviction="medium",
        pillars=[{"text": core_logic[:100], "falsification": "待补充", "weight": 1.0}],
    )


async def check_thesis_validity(
    store: WatchlistStore,
    thesis_id: str,
) -> dict:
    """检查单个论点的有效性：对比支柱证伪条件与当前市场数据。

    Returns:
        {"status": str, "weakened_pillars": list, "broken_pillars": list}
    """
    thesis = store.get_thesis(thesis_id)
    if not thesis:
        return {"status": "not_found", "weakened_pillars": [], "broken_pillars": []}

    pillars = store.get_pillars(thesis_id)
    ticker = thesis["ticker"]

    snapshots = store.get_snapshots(ticker, days=30)
    latest_snap = snapshots[-1] if snapshots else {}

    weakened = []
    broken = []

    for pillar in pillars:
        if pillar["status"] == "broken":
            broken.append(pillar["pillar_text"])
            continue

        falsification = pillar.get("falsification", "")
        if not falsification or falsification == "待补充":
            continue

        evidence = store.get_evidence_log(thesis_id, limit=10)
        contradicting = [e for e in evidence
                         if e.get("pillar_id") == pillar["id"]
                         and e.get("direction") == "contradicting"]

        if len(contradicting) >= 3:
            store.update_pillar_status(pillar["id"], "weakened")
            weakened.append(pillar["pillar_text"])
        elif len(contradicting) >= 5:
            store.update_pillar_status(pillar["id"], "broken")
            broken.append(pillar["pillar_text"])

    total = len(pillars)
    broken_count = len(broken)
    weakened_count = len(weakened)

    new_conviction = thesis["conviction"]
    if broken_count > 0:
        new_conviction = "low"
    elif weakened_count >= total / 2:
        if thesis["conviction"] == "high":
            new_conviction = "medium"
        elif thesis["conviction"] == "medium":
            new_conviction = "low"

    new_status = thesis["status"]
    if broken_count >= total / 2:
        new_status = "invalidated"
    elif weakened_count > 0 and new_conviction == "low":
        new_status = "weakened"

    if new_conviction != thesis["conviction"] or new_status != thesis["status"]:
        store.update_thesis_status(thesis_id, new_status, new_conviction)
        logger.info("论点状态变更 %s: %s → %s, 信心 %s → %s",
                     ticker, thesis["status"], new_status,
                     thesis["conviction"], new_conviction)

    return {
        "status": new_status,
        "conviction": new_conviction,
        "weakened_pillars": weakened,
        "broken_pillars": broken,
    }


async def check_all_theses(
    store: WatchlistStore,
) -> AsyncGenerator[dict, None]:
    """检查所有活跃论点的有效性"""
    entries = store.list_all()
    total_checked = 0
    issues = []

    for entry in entries:
        theses = store.get_theses_for_entry(entry["id"], active_only=True)
        for thesis in theses:
            result = await check_thesis_validity(store, thesis["id"])
            total_checked += 1
            if result["weakened_pillars"] or result["broken_pillars"]:
                issues.append({
                    "ticker": thesis["ticker"],
                    "thesis": thesis["thesis_title"],
                    "weakened": result["weakened_pillars"],
                    "broken": result["broken_pillars"],
                    "new_conviction": result["conviction"],
                })

    yield _sse("thesis_check_done",
               checked=total_checked, issues=len(issues),
               details=issues,
               message=f"论点检查完成: {total_checked} 个论点, {len(issues)} 个问题")


async def run_quarterly_review(
    store: WatchlistStore,
    budget=None,
) -> AsyncGenerator[dict, None]:
    """季度强制审查：不管有无新进展，对所有活跃论点进行全面评估"""
    stale = store.get_stale_theses(days=90)
    all_active_theses = []
    for entry in store.list_all():
        all_active_theses.extend(store.get_theses_for_entry(entry["id"], active_only=True))

    review_targets = stale if stale else all_active_theses
    total = len(review_targets)

    yield _sse("quarterly_review_start", total=total,
               message=f"开始季度审查 {total} 个论点...")

    llm, provider, model = get_llm_for_position(position="watchlist_thesis", temperature=0.2)
    reviewed = 0

    for thesis in review_targets:
        ticker = thesis["ticker"]
        thesis_id = thesis["id"]
        pillars = store.get_pillars(thesis_id)
        evidence = store.get_evidence_log(thesis_id, limit=20)

        if llm and budget and budget.can_spend(estimated_tokens=2000):
            try:
                result = await _llm_review_thesis(
                    llm, thesis, pillars, evidence, store, budget, provider, model,
                )
                if result:
                    for pa in result.get("pillar_assessments", []):
                        pid = pa.get("pillar_id", "")
                        new_status = pa.get("current_status", "intact")
                        if pid and new_status in ("intact", "weakened", "broken"):
                            store.update_pillar_status(pid, new_status)

                    new_conviction = result.get("overall_conviction", thesis["conviction"])
                    if new_conviction != thesis["conviction"]:
                        store.update_thesis_status(thesis_id, thesis["status"], new_conviction)
                        store.create_evidence(
                            thesis_id=thesis_id,
                            pillar_id=None,
                            date=_today(),
                            data_point=f"季度审查: {result.get('conviction_change_reason', '')}",
                            direction="neutral",
                            thesis_impact="weakens" if new_conviction == "low" else "no_change",
                            conviction_before=thesis["conviction"],
                            conviction_after=new_conviction,
                            source="quarterly_review",
                        )

                    reviewed += 1
                    yield _sse("thesis_reviewed", ticker=ticker,
                               conviction=new_conviction,
                               action=result.get("recommended_action", "hold"),
                               message=f"{ticker} 审查完成: {new_conviction}")
            except Exception as e:
                logger.warning("论点审查失败 %s: %s", ticker, e)
        else:
            validity = await check_thesis_validity(store, thesis_id)
            reviewed += 1
            yield _sse("thesis_reviewed", ticker=ticker,
                       conviction=validity.get("conviction", thesis["conviction"]),
                       action="hold",
                       message=f"{ticker} 规则审查完成")

    yield _sse("quarterly_review_done", reviewed=reviewed,
               message=f"季度审查完成: {reviewed}/{total} 个论点")


async def _llm_review_thesis(
    llm, thesis: dict, pillars: list[dict], evidence: list[dict],
    store: WatchlistStore, budget, provider: str, model: str,
) -> dict | None:
    ticker = thesis["ticker"]

    pillars_text = ""
    for i, p in enumerate(pillars, 1):
        pillars_text += f"\n### 支柱{i} (ID: {p['id']}, 权重: {p['weight']})\n"
        pillars_text += f"- 论述: {p['pillar_text']}\n"
        pillars_text += f"- 证伪条件: {p['falsification']}\n"
        pillars_text += f"- 当前状态: {p['status']}\n"

    evidence_text = ""
    for e in evidence[:10]:
        evidence_text += f"- [{e['date']}] {e['data_point']} ({e['direction']}) → {e['thesis_impact']}\n"
    if not evidence_text:
        evidence_text = "暂无证据记录"

    snapshots = store.get_snapshots(ticker, days=30)
    market_context = ""
    if snapshots:
        latest = snapshots[-1]
        market_context = (
            f"最新收盘价: {latest.get('close', 'N/A')}, "
            f"RSI: {latest.get('rsi_14', 'N/A')}, "
            f"涨跌: {latest.get('change_pct', 'N/A')}%"
        )

    prompt_path = PROMPTS_DIR / "thesis_review.md"
    template = prompt_path.read_text(encoding="utf-8")
    prompt = template.replace("{ticker}", ticker)
    prompt = prompt.replace("{thesis_title}", thesis.get("thesis_title", ""))
    prompt = prompt.replace("{thesis_summary}", thesis.get("thesis_summary", ""))
    prompt = prompt.replace("{conviction}", thesis.get("conviction", "medium"))
    prompt = prompt.replace("{created_at}", thesis.get("created_at", ""))
    prompt = prompt.replace("{pillars_text}", pillars_text)
    prompt = prompt.replace("{evidence_text}", evidence_text)
    prompt = prompt.replace("{market_context}", market_context)

    response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
    if budget:
        budget.record(provider, model, 1200, 800, "thesis_review")

    return extract_json_object(response)


def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
