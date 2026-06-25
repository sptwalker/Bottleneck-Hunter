"""调优建议引擎 — 分析历史复盘数据，LLM 生成参数调优建议。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from bottleneck_hunter.chain.json_utils import extract_json_object
from bottleneck_hunter.watchlist.store import WatchlistStore

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
            return create_llm(p, m, temperature=0.3), p, m

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


async def generate_tuning_suggestions(
    store: WatchlistStore,
    budget=None,
) -> AsyncGenerator[dict, None]:
    """分析复盘记录，LLM 生成调优建议"""
    yield _sse("tuning_start", message="开始分析交易历史...")

    reviews = store.get_auto_reviews(limit=20)
    if len(reviews) < 3:
        yield _sse("tuning_done", suggestions=[], message="复盘记录不足（需至少 3 条），暂无法生成调优建议")
        return

    from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator
    calc = PerformanceCalculator(store)
    overview = calc.compute_overview()
    review_summary = calc.compute_review_summary()

    reviews_text = []
    for r in reviews[:15]:
        rj = r.get("result_json", {})
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = {}
        reviews_text.append(
            f"- {r.get('ticker', '?')}: 收益 {r.get('return_pct', 0):+.1f}%, "
            f"质量 {rj.get('trade_quality_score', '?')}/10, "
            f"教训: {'; '.join(rj.get('key_lessons', [])[:2])}"
        )

    lessons_text = []
    for item in review_summary.get("common_lessons", []):
        lessons_text.append(f"- {item['lesson']}（出现 {item['count']} 次）")

    llm, provider, model = _get_llm()
    if not llm:
        yield _sse("tuning_error", error="无可用 LLM")
        return

    if budget and not budget.can_spend(estimated_tokens=3000):
        yield _sse("tuning_error", error="预算不足，跳过调优建议生成")
        return

    yield _sse("tuning_progress", message="LLM 分析中...")

    try:
        prompt_template = (PROMPTS_DIR / "tuning_suggestions.md").read_text(encoding="utf-8")
        prompt = (prompt_template
                  .replace("{performance_overview}",
                           f"总交易: {overview['total_trades']}, 胜率: {overview['win_rate']}%, "
                           f"总收益: {overview['total_return_pct']}%, 平均收益: {overview['avg_return_pct']}%, "
                           f"最佳: {overview['best_trade_pct']}%, 最差: {overview['worst_trade_pct']}%")
                  .replace("{reviews_summary}", "\n".join(reviews_text) or "无复盘记录")
                  .replace("{common_lessons}", "\n".join(lessons_text) or "无常见教训"))

        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        if budget:
            budget.record(provider, model, 2000, 1000, "tuning_suggestions")

        result = extract_json_object(response)
        suggestions = result.get("suggestions", [])

        saved = []
        for s in suggestions[:5]:
            tid = store.create_tuning_proposal(
                type_=s.get("type", "weight"),
                parameter_name=s.get("parameter", ""),
                old_value=s.get("current", ""),
                new_value=s.get("suggested", ""),
                reason=s.get("reason", ""),
                evidence=s.get("evidence", []),
            )
            saved.append({**s, "id": tid})

        yield _sse("tuning_done",
                    analysis=result.get("analysis", ""),
                    suggestions=saved,
                    message=f"生成 {len(saved)} 条调优建议")

    except Exception as e:
        logger.exception("调优建议生成失败")
        yield _sse("tuning_error", error=str(e))
