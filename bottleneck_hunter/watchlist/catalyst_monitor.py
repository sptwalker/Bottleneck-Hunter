"""催化剂监控器 — 检测、生命周期管理、到期处理

从 strategy_records 和 LLM 识别催化剂事件，跟踪状态变化，
为 L3 战术计划提供催化剂时间线输入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncGenerator

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.chain.json_utils import extract_json_object

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


def _get_llm():
    try:
        from bottleneck_hunter.llm_clients.factory import create_llm
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


async def check_catalyst_expiry(
    store: WatchlistStore,
) -> AsyncGenerator[dict, None]:
    """检查并自动过期超过预期日期的催化剂，提醒即将到期的催化剂"""
    expired_count = store.expire_past_catalysts()
    if expired_count > 0:
        yield _sse("catalyst_expired", count=expired_count,
                    message=f"已自动过期 {expired_count} 个超期催化剂")

    expiring = store.get_expiring_catalysts(days=7)
    if expiring:
        tickers = list({c["ticker"] for c in expiring})
        yield _sse("catalyst_expiring_soon", count=len(expiring),
                    tickers=tickers,
                    message=f"{len(expiring)} 个催化剂将在 7 天内到期: {', '.join(tickers[:5])}")


async def detect_catalysts(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """扫描观察池，从已有策略和情报中提取催化剂事件"""
    entries = store.list_all()
    total = len(entries)
    yield _sse("catalyst_scan_start", total=total, message=f"开始扫描 {total} 只股票的催化剂...")

    llm, provider, model = _get_llm()
    created = 0

    for entry in entries:
        entry_id = entry["id"]
        ticker = entry["ticker"]

        existing = store.get_catalysts_for_entry(entry_id, active_only=True)
        if len(existing) >= 5:
            continue

        strategy = store.get_latest_strategy(entry_id)
        intel = store.get_latest_intelligence(entry_id)
        if not strategy and not intel:
            continue

        if llm and budget and budget.can_spend(estimated_tokens=1500):
            try:
                catalysts = await _extract_catalysts_llm(
                    llm, ticker, strategy, intel, existing, budget, provider, model
                )
                for cat in catalysts:
                    store.create_catalyst(
                        entry_id=entry_id,
                        ticker=ticker,
                        title=cat.get("title", ""),
                        catalyst_type=cat.get("type", "event"),
                        description=cat.get("description", ""),
                        expected_date=cat.get("expected_date"),
                        impact_level=cat.get("impact_level", "medium"),
                        confidence=cat.get("confidence", 5),
                    )
                    created += 1

                yield _sse("catalyst_detected", ticker=ticker, count=len(catalysts),
                           message=f"{ticker} 发现 {len(catalysts)} 个催化剂")
            except Exception as e:
                logger.warning("催化剂提取失败 %s: %s", ticker, e)
        else:
            catalysts = _extract_catalysts_from_strategy(strategy)
            for cat in catalysts:
                dup = any(c["title"] == cat["title"] for c in existing)
                if not dup:
                    store.create_catalyst(
                        entry_id=entry_id, ticker=ticker,
                        title=cat["title"],
                        catalyst_type=cat.get("type", "event"),
                        expected_date=cat.get("expected_date"),
                        impact_level=cat.get("impact_level", "medium"),
                    )
                    created += 1

    yield _sse("catalyst_scan_done", created=created,
               message=f"催化剂扫描完成，新增 {created} 个")


async def _extract_catalysts_llm(
    llm, ticker: str, strategy: dict | None, intel: dict | None,
    existing: list[dict], budget: BudgetTracker, provider: str, model: str,
) -> list[dict]:
    """LLM 提取催化剂"""
    context_parts = [f"股票: {ticker}"]
    if strategy:
        core = strategy.get("core_logic", "")
        action = strategy.get("action_strategy", "{}")
        if isinstance(action, str):
            action = action[:500]
        context_parts.append(f"核心逻辑: {core}")
        context_parts.append(f"操作策略: {action}")
    if intel:
        brief = intel.get("brief_text", "")
        if brief:
            context_parts.append(f"情报简报: {brief}")
        news = intel.get("news_summary", "{}")
        if isinstance(news, str):
            try:
                news = json.loads(news)
            except (json.JSONDecodeError, TypeError):
                news = {}
        if news.get("recent_titles"):
            context_parts.append(f"近期新闻: {', '.join(news['recent_titles'][:3])}")

    existing_titles = [c["title"] for c in existing]

    prompt = f"""你是投资催化剂分析师。请从以下信息中提取 {ticker} 未来 1-3 个月可能影响股价的关键催化剂事件。

{chr(10).join(context_parts)}

已有催化剂（不要重复）：{json.dumps(existing_titles, ensure_ascii=False)}

返回 JSON 数组（最多 3 个），每项包含：
- title: 催化剂标题（简短）
- type: earnings / product / policy / capacity / technical / other
- description: 简要描述
- expected_date: 预期日期（YYYY-MM-DD 或 null）
- impact_level: low / medium / high / critical
- confidence: 1-10

返回 JSON 数组，不要多余文字。"""

    response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
    budget.record(provider, model, 800, 500, "catalyst_detect")

    text = response.strip()
    if text.startswith("["):
        return json.loads(text)
    from bottleneck_hunter.chain.json_utils import extract_json_array
    arr = extract_json_array(text)
    return arr if arr else []


def _extract_catalysts_from_strategy(strategy: dict | None) -> list[dict]:
    """从策略文本中简单提取催化剂关键词"""
    if not strategy:
        return []

    catalysts = []
    action = strategy.get("action_strategy", "{}")
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except (json.JSONDecodeError, TypeError):
            action = {}

    if isinstance(action, dict):
        for key in ("催化剂", "catalyst", "关键事件", "key_events"):
            val = action.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, str):
                        catalysts.append({"title": item, "type": "event"})
                    elif isinstance(item, dict):
                        catalysts.append({
                            "title": item.get("event", item.get("title", str(item))),
                            "type": item.get("type", "event"),
                            "expected_date": item.get("date") or item.get("expected_date"),
                            "impact_level": item.get("impact", "medium"),
                        })
            elif isinstance(val, str) and val:
                catalysts.append({"title": val, "type": "event"})

    return catalysts[:3]
