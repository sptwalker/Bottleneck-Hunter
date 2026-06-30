"""tuning_engine 单元测试。

覆盖 _sse / _get_llm / generate_tuning_suggestions 的各种分支。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist import tuning_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(gen):
    """消费异步生成器，返回列表。"""
    items = []
    async for item in gen:
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# TestSseHelper
# ---------------------------------------------------------------------------


class TestSseHelper:
    def test_sse_basic(self):
        result = tuning_engine._sse("test_event", message="hello")
        assert result["event"] == "test_event"
        assert result["data"]["event"] == "test_event"
        assert result["data"]["message"] == "hello"

    def test_sse_multiple_fields(self):
        result = tuning_engine._sse("ev", a=1, b="two")
        assert result["data"]["a"] == 1
        assert result["data"]["b"] == "two"



# ---------------------------------------------------------------------------
# TestGenerateTuningSuggestions
# ---------------------------------------------------------------------------


class TestGenerateTuningSuggestions:
    """测试 generate_tuning_suggestions 的各种执行路径。"""

    async def test_insufficient_reviews(self, tmp_path):
        """复盘记录不足 3 条时，直接返回 tuning_done + 空 suggestions。"""
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "test.db")

        events = await _collect(tuning_engine.generate_tuning_suggestions(store))
        assert events[0]["event"] == "tuning_start"
        done = events[-1]
        assert done["event"] == "tuning_done"
        assert done["data"]["suggestions"] == []
        assert "不足" in done["data"]["message"]

    async def test_no_llm_yields_error(self, tmp_path):
        """有足够复盘但无可用 LLM 时，返回 tuning_error。"""
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "test.db")

        mock_reviews = [
            {"ticker": f"T{i}", "return_pct": i * 1.0, "result_json": json.dumps({"trade_quality_score": 7, "key_lessons": ["lesson"]})}
            for i in range(5)
        ]
        with patch.object(store, "get_auto_reviews", return_value=mock_reviews), \
             patch("bottleneck_hunter.watchlist.tuning_engine.get_llm_for_position", return_value=(None, "", "")):
            events = await _collect(tuning_engine.generate_tuning_suggestions(store))

        event_types = [e["event"] for e in events]
        assert "tuning_error" in event_types
        err = next(e for e in events if e["event"] == "tuning_error")
        assert "无可用 LLM" in err["data"]["error"]

    async def test_budget_insufficient(self, tmp_path):
        """预算不足时返回 tuning_error。"""
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "test.db")

        mock_reviews = [{"ticker": f"T{i}", "return_pct": 0, "result_json": "{}"} for i in range(5)]
        mock_budget = MagicMock()
        mock_budget.can_spend.return_value = False
        mock_llm = MagicMock()

        with patch.object(store, "get_auto_reviews", return_value=mock_reviews), \
             patch("bottleneck_hunter.watchlist.tuning_engine.get_llm_for_position", return_value=(mock_llm, "test", "model")):
            events = await _collect(tuning_engine.generate_tuning_suggestions(store, budget=mock_budget))

        event_types = [e["event"] for e in events]
        assert "tuning_error" in event_types

    async def test_success_flow(self, tmp_path):
        """正常流程：reviews 足够 → LLM 返回 JSON → 存储建议。"""
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "test.db")

        mock_reviews = [
            {"ticker": f"T{i}", "return_pct": i * 2.0,
             "result_json": json.dumps({"trade_quality_score": 6, "key_lessons": ["少追高"]})}
            for i in range(5)
        ]
        mock_overview = {"total_trades": 10, "win_rate": 60, "total_return_pct": 15.5,
                         "avg_return_pct": 3.1, "best_trade_pct": 20.0, "worst_trade_pct": -8.0}
        mock_review_summary = {"common_lessons": [{"lesson": "少追高", "count": 3}]}

        llm_response = json.dumps({
            "analysis": "整体表现尚可",
            "suggestions": [
                {"type": "weight", "parameter": "止损线", "current": "-10%", "suggested": "-7%",
                 "reason": "减少亏损", "evidence": ["历史最差 -8%"]},
            ]
        })
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content=llm_response)

        mock_calc = MagicMock()
        mock_calc.compute_overview.return_value = mock_overview
        mock_calc.compute_review_summary.return_value = mock_review_summary

        prompt_text = "{performance_overview}\n{reviews_summary}\n{common_lessons}"

        with patch.object(store, "get_auto_reviews", return_value=mock_reviews), \
             patch("bottleneck_hunter.watchlist.tuning_engine.get_llm_for_position", return_value=(mock_llm, "test", "model")), \
             patch("bottleneck_hunter.watchlist.performance_stats.PerformanceCalculator", return_value=mock_calc), \
             patch("bottleneck_hunter.watchlist.tuning_engine.PROMPTS_DIR") as mock_dir, \
             patch("bottleneck_hunter.watchlist.tuning_engine.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            (mock_dir / "tuning_suggestions.md").read_text.return_value = prompt_text
            mock_thread.return_value = llm_response

            with patch.object(store, "create_tuning_proposal", return_value=1):
                events = await _collect(tuning_engine.generate_tuning_suggestions(store))

        event_types = [e["event"] for e in events]
        assert "tuning_start" in event_types
        assert "tuning_done" in event_types
        done = next(e for e in events if e["event"] == "tuning_done")
        assert len(done["data"]["suggestions"]) == 1

    async def test_llm_exception(self, tmp_path):
        """LLM 调用抛异常时返回 tuning_error。"""
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "test.db")

        mock_reviews = [{"ticker": f"T{i}", "return_pct": 0, "result_json": "{}"} for i in range(5)]
        mock_llm = MagicMock()
        mock_calc = MagicMock()
        mock_calc.compute_overview.return_value = {"total_trades": 5, "win_rate": 50,
                                                    "total_return_pct": 0, "avg_return_pct": 0,
                                                    "best_trade_pct": 0, "worst_trade_pct": 0}
        mock_calc.compute_review_summary.return_value = {"common_lessons": []}

        prompt_text = "{performance_overview}\n{reviews_summary}\n{common_lessons}"

        with patch.object(store, "get_auto_reviews", return_value=mock_reviews), \
             patch("bottleneck_hunter.watchlist.tuning_engine.get_llm_for_position", return_value=(mock_llm, "test", "model")), \
             patch("bottleneck_hunter.watchlist.performance_stats.PerformanceCalculator", return_value=mock_calc), \
             patch("bottleneck_hunter.watchlist.tuning_engine.PROMPTS_DIR") as mock_dir, \
             patch("bottleneck_hunter.watchlist.tuning_engine.asyncio.to_thread", new_callable=AsyncMock, side_effect=RuntimeError("LLM 挂了")):
            (mock_dir / "tuning_suggestions.md").read_text.return_value = prompt_text

            events = await _collect(tuning_engine.generate_tuning_suggestions(store))

        event_types = [e["event"] for e in events]
        assert "tuning_error" in event_types
