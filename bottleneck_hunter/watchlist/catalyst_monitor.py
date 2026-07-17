"""催化剂监控器 — 检测、生命周期管理、到期处理

从 strategy_records 和 LLM 识别催化剂事件，跟踪状态变化，
为 L3 战术计划提供催化剂时间线输入。
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
from bottleneck_hunter.llm_clients.factory import get_llm_for_position, get_models_for_role

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "chain" / "prompts"


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": data}


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

    llm, provider, model = get_llm_for_position(position="watchlist_catalyst")
    all_catalyst_models = get_models_for_role("watchlist_catalyst")
    second_llm = all_catalyst_models[1][0] if len(all_catalyst_models) >= 2 else None
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

        # 跳过重复提取：策略与情报自上次提取以来都没更新（源没变→重提结果一样）。
        # 策略每周才变、催化剂每天扫 → 多数日子源未变，跳过省 LLM。保守：仅在"有历史催化剂
        # 可作参照 且 源时间 ≤ 最新催化剂时间"时才跳，任何不确定(无历史/缺时间戳/源更新)都照跑。
        if existing:
            newest_cat_ts = max((c.get("created_at", "") for c in existing), default="")
            src_ts = max((strategy or {}).get("created_at", ""), (intel or {}).get("created_at", ""))
            if newest_cat_ts and src_ts and src_ts <= newest_cat_ts:
                logger.debug("催化剂：%s 策略/情报自上次提取未更新，跳过", ticker)
                continue

        if llm and budget and budget.can_spend(estimated_tokens=1500):
            try:
                catalysts = await _extract_catalysts_llm(
                    llm, ticker, strategy, intel, existing, budget, provider, model
                )
                for cat in catalysts:
                    if second_llm and cat.get("impact_level") in ("high", "critical"):
                        confirmed = await _cross_confirm_catalyst(
                            second_llm, ticker, cat, budget,
                            all_catalyst_models[1][1], all_catalyst_models[1][2],
                        )
                        if not confirmed:
                            cat["confidence"] = max(1, cat.get("confidence", 5) - 3)
                            cat["_cross_rejected"] = True
                            logger.info("高影响催化剂 %s 未通过交叉确认，置信度下调", cat.get("title"))

                    store.create_catalyst(
                        entry_id=entry_id,
                        ticker=ticker,
                        title=cat.get("title", ""),
                        catalyst_type=cat.get("type", "event"),
                        description=cat.get("description", ""),
                        expected_date=cat.get("expected_date"),
                        impact_level=cat.get("impact_level", "medium"),
                        confidence=cat.get("confidence", 5),
                        source_category=cat.get("source_category", "other"),
                        impact_color=cat.get("impact_color", "yellow"),
                        direction=cat.get("direction", "neutral"),
                        time_window=cat.get("time_window", ""),
                        position_implication=cat.get("position_implication", ""),
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
- source_category: 来源维度 (earnings / corporate / industry / macro)
- impact_color: 影响颜色 (red=高冲击 / yellow=中等 / green=常规)
- direction: 方向 (bullish / neutral / bearish)
- time_window: 时间窗口描述（如 "2026-07-15 ± 3天"）
- position_implication: 对头寸的影响建议（如 "利好持仓，可加仓" 或 "风险事件，建议减仓对冲"）

返回 JSON 数组，不要多余文字。"""

    response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
    budget.record(provider, model, 800, 500, "catalyst_detect")

    text = response.strip()
    if text.startswith("["):
        return json.loads(text)
    from bottleneck_hunter.chain.json_utils import extract_json_array
    arr = extract_json_array(text)
    return arr if arr else []


async def _cross_confirm_catalyst(
    llm, ticker: str, catalyst: dict,
    budget: BudgetTracker, provider: str, model: str,
) -> bool:
    """用第二个模型独立验证高影响催化剂的合理性。"""
    prompt = f"""请独立评估以下催化剂事件的真实性和影响程度。

股票: {ticker}
催化剂标题: {catalyst.get('title', '')}
描述: {catalyst.get('description', '')}
影响等级: {catalyst.get('impact_level', 'medium')}
置信度: {catalyst.get('confidence', 5)}/10

请返回 JSON:
{{"confirmed": true/false, "adjusted_confidence": 1-10, "reason": "简要说明"}}
仅当你认为该催化剂确实存在且影响等级合理时返回 confirmed=true。"""

    try:
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        if budget:
            budget.record(provider, model, 400, 200, "catalyst_cross_confirm")
        result = extract_json_object(response)
        return result.get("confirmed", True)
    except Exception as e:
        logger.warning("催化剂交叉确认失败: %s", e)
        return True


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


# ---------------------------------------------------------------------------
# 催化剂结果判定 (17D.4)
# ---------------------------------------------------------------------------

def judge_catalyst_outcome(
    store: WatchlistStore, ticker: str, catalyst: dict,
) -> dict:
    """基于规则判定催化剂是否已兑现。

    收集事件预期日期前后 5 天的价格变化和新闻标题，
    不调用 LLM，用简单规则判定。

    返回:
        {"outcome": "realized"|"failed"|"partial",
         "impact": float (-5 ~ +5),
         "reason": str}
    """
    expected_date = catalyst.get("expected_date", "")
    if not expected_date:
        return {"outcome": "failed", "impact": 0, "reason": "无预期日期，无法判定"}

    impact_level = catalyst.get("impact_level", "medium")
    catalyst_type = catalyst.get("catalyst_type", "event")
    title = catalyst.get("title", "")

    # ── 收集价格数据：预期日期前后 5 天 ──
    snapshots = store.get_snapshots(ticker, days=30)
    if not snapshots:
        return {"outcome": "partial", "impact": 0, "reason": "无价格数据，无法确认"}

    # 按日期排序（升序）
    snapshots.sort(key=lambda s: s.get("date", ""))

    # 找到预期日期前后各 5 天的快照
    before_price = None
    after_price = None
    for snap in snapshots:
        snap_date = snap.get("date", "")
        if not snap_date:
            continue
        if snap_date <= expected_date and snap.get("close"):
            before_price = snap.get("close")
        if snap_date > expected_date and snap.get("close") and after_price is None:
            after_price = snap.get("close")

    # 计算价格变动
    price_change_pct = 0.0
    if before_price and after_price and before_price > 0:
        price_change_pct = ((after_price - before_price) / before_price) * 100

    # ── 收集新闻标题：检查是否有相关新闻 ──
    news_items = store.get_news(ticker, limit=30)
    related_news_count = 0
    title_keywords = [kw for kw in title.lower().replace("，", " ").replace(",", " ").split() if len(kw) >= 2]
    for news in news_items:
        news_date = news.get("date", "")
        # 只看预期日期前后 5 天的新闻
        if not news_date or abs(_date_diff(news_date, expected_date)) > 5:
            continue
        news_title = news.get("title", "").lower()
        if any(kw in news_title for kw in title_keywords):
            related_news_count += 1

    # ── 判定规则 ──
    # 影响度基准（根据 impact_level）
    impact_base = {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(impact_level, 2)

    abs_change = abs(price_change_pct)

    # 规则 1：价格变动超过 3% 且有相关新闻 → realized
    if abs_change >= 3.0 and related_news_count >= 1:
        # 判断方向：催化剂通常是正面事件
        direction = 1 if price_change_pct > 0 else -1
        impact = direction * min(abs_change / 2, 5)  # 归一化到 -5 ~ +5
        return {
            "outcome": "realized",
            "impact": round(impact, 1),
            "reason": f"价格变动 {price_change_pct:+.1f}%，{related_news_count} 条相关新闻"
        }

    # 规则 2：价格变动超过 3% 但无相关新闻 → partial
    if abs_change >= 3.0:
        direction = 1 if price_change_pct > 0 else -1
        impact = direction * min(abs_change / 3, 3)
        return {
            "outcome": "partial",
            "impact": round(impact, 1),
            "reason": f"价格变动 {price_change_pct:+.1f}%，但无直接相关新闻佐证"
        }

    # 规则 3：有相关新闻但价格变动不大 → partial
    if related_news_count >= 1 and abs_change >= 1.0:
        direction = 1 if price_change_pct > 0 else -1
        impact = direction * impact_base * 0.5
        return {
            "outcome": "partial",
            "impact": round(impact, 1),
            "reason": f"有 {related_news_count} 条相关新闻，但价格仅变动 {price_change_pct:+.1f}%"
        }

    # 规则 4：无显著变动，无相关新闻 → failed
    return {
        "outcome": "failed",
        "impact": 0,
        "reason": f"价格变动 {price_change_pct:+.1f}%，无相关新闻，事件可能未发生"
    }


def _date_diff(date_a: str, date_b: str) -> int:
    """计算两个 YYYY-MM-DD 日期之间的天数差（a - b）"""
    try:
        from datetime import datetime as dt
        da = dt.strptime(date_a[:10], "%Y-%m-%d")
        db = dt.strptime(date_b[:10], "%Y-%m-%d")
        return (da - db).days
    except (ValueError, TypeError):
        return 999


async def judge_expired_catalysts(
    store: WatchlistStore,
) -> AsyncGenerator[dict, None]:
    """批量判定已过期但未判定的催化剂"""
    unjudged = store.get_unjudged_expired_catalysts()
    if not unjudged:
        yield _sse("catalyst_judge_done", judged=0, message="无需判定的催化剂")
        return

    yield _sse("catalyst_judge_start", total=len(unjudged),
               message=f"开始判定 {len(unjudged)} 个过期催化剂...")

    judged_count = 0
    for catalyst in unjudged:
        ticker = catalyst.get("ticker", "")
        cid = catalyst.get("id", "")
        if not ticker or not cid:
            continue

        try:
            result = judge_catalyst_outcome(store, ticker, catalyst)
            store.judge_catalyst(
                catalyst_id=cid,
                outcome=result["outcome"],
                impact=result["impact"],
                actual_date=catalyst.get("expected_date"),
            )
            judged_count += 1
            logger.info("催化剂 %s (%s) 判定: %s, 影响度: %s — %s",
                        cid, ticker, result["outcome"], result["impact"], result["reason"])
        except Exception as e:
            logger.warning("催化剂判定失败 %s (%s): %s", cid, ticker, e)

    yield _sse("catalyst_judge_done", judged=judged_count,
               message=f"催化剂判定完成：{judged_count}/{len(unjudged)} 个")


# ---------------------------------------------------------------------------
# Phase 20B: 催化剂日历 + 周度前瞻
# ---------------------------------------------------------------------------

def get_catalyst_calendar(store: WatchlistStore, days: int = 30) -> dict:
    """按日期+来源维度组织催化剂日历视图。

    返回:
        {"dates": {"2026-07-01": [...], ...},
         "by_category": {"earnings": [...], ...},
         "summary": {"total": N, "red": N, "yellow": N, "green": N}}
    """
    from datetime import datetime, timezone, timedelta

    catalysts = store.get_upcoming_catalysts(days=days)

    dates: dict[str, list] = {}
    by_category: dict[str, list] = {}
    color_counts = {"red": 0, "yellow": 0, "green": 0}

    for c in catalysts:
        date_key = (c.get("expected_date") or "未定")[:10]
        item = {
            "id": c.get("id"),
            "ticker": c.get("ticker", ""),
            "title": c.get("title", ""),
            "catalyst_type": c.get("catalyst_type", "event"),
            "impact_level": c.get("impact_level", "medium"),
            "source_category": c.get("source_category", "other"),
            "impact_color": c.get("impact_color", "yellow"),
            "direction": c.get("direction", "neutral"),
            "time_window": c.get("time_window", ""),
            "position_implication": c.get("position_implication", ""),
            "confidence": c.get("confidence", 5),
        }

        dates.setdefault(date_key, []).append(item)

        cat = item["source_category"]
        by_category.setdefault(cat, []).append(item)

        color = item["impact_color"]
        if color in color_counts:
            color_counts[color] += 1

    return {
        "dates": dict(sorted(dates.items())),
        "by_category": by_category,
        "summary": {
            "total": len(catalysts),
            **color_counts,
        },
    }


def generate_weekly_preview(store: WatchlistStore) -> dict:
    """生成未来 7 天催化剂周度前瞻。

    按 source_category 分组，impact_color 排序（red > yellow > green）。

    返回:
        {"week_start": "2026-06-27", "week_end": "2026-07-03",
         "categories": {"earnings": [...], "macro": [...]},
         "highlights": [...], "total": N}
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    week_start = now.strftime("%Y-%m-%d")
    week_end = (now + timedelta(days=7)).strftime("%Y-%m-%d")

    catalysts = store.get_upcoming_catalysts(days=7)

    color_order = {"red": 0, "yellow": 1, "green": 2}
    catalysts.sort(key=lambda c: color_order.get(c.get("impact_color", "yellow"), 1))

    categories: dict[str, list] = {}
    highlights = []

    for c in catalysts:
        cat = c.get("source_category", "other")
        item = {
            "ticker": c.get("ticker", ""),
            "title": c.get("title", ""),
            "expected_date": c.get("expected_date", ""),
            "impact_color": c.get("impact_color", "yellow"),
            "direction": c.get("direction", "neutral"),
            "position_implication": c.get("position_implication", ""),
        }
        categories.setdefault(cat, []).append(item)

        if c.get("impact_color") == "red" or c.get("impact_level") in ("high", "critical"):
            highlights.append(item)

    return {
        "week_start": week_start,
        "week_end": week_end,
        "categories": categories,
        "highlights": highlights[:5],
        "total": len(catalysts),
    }
