"""Tests for uzi_runner.py — UZI 分析引擎。

覆盖辅助函数、4 种分析类型（deep/panel/lhb/trap）、LLM 回退逻辑和异常处理。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist import uzi_runner
from bottleneck_hunter.watchlist.uzi_runner import (
    ANALYSIS_TYPES,
    _extract_summary,
    _mock_deep_analysis,
    _mock_investor_panel,
    _mock_trap_result,
    _type_label,
    run_uzi_analysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_store():
    store = MagicMock()
    store.create_uzi_analysis.return_value = "test_analysis_id"
    return store


async def _collect(gen):
    """消费异步生成器，返回所有事件列表。"""
    events = []
    async for ev in gen:
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# TestTypeLabel — _type_label
# ---------------------------------------------------------------------------

class TestTypeLabel:
    def test_deep_analysis(self):
        assert _type_label("deep-analysis") == "深度分析"

    def test_investor_panel(self):
        assert _type_label("investor-panel") == "投资者评审"

    def test_lhb_analyzer(self):
        assert _type_label("lhb-analyzer") == "龙虎榜分析"

    def test_trap_detector(self):
        assert _type_label("trap-detector") == "杀猪盘检测"

    def test_unknown_returns_raw(self):
        assert _type_label("something-else") == "something-else"


# ---------------------------------------------------------------------------
# TestExtractSummary — _extract_summary
# ---------------------------------------------------------------------------

class TestExtractSummary:
    def test_deep_analysis(self):
        result = {"overall_score": 7.5}
        assert _extract_summary("deep-analysis", result) == "综合评分 7.5/10"

    def test_deep_analysis_missing(self):
        assert "?" in _extract_summary("deep-analysis", {})

    def test_investor_panel(self):
        result = {"panel_consensus": 85.0}
        assert _extract_summary("investor-panel", result) == "共识度 85.0%"

    def test_trap_detector(self):
        result = {"trap_level": "🟢 安全"}
        assert _extract_summary("trap-detector", result) == "🟢 安全"

    def test_lhb_analyzer(self):
        result = {"summary": "近期共 5 条龙虎榜记录"}
        assert _extract_summary("lhb-analyzer", result) == "近期共 5 条龙虎榜记录"

    def test_lhb_default(self):
        assert _extract_summary("lhb-analyzer", {}) == "分析完成"

    def test_unknown_type(self):
        assert _extract_summary("unknown-type", {}) == ""




# ---------------------------------------------------------------------------
# TestMockFunctions — mock 数据工厂
# ---------------------------------------------------------------------------

class TestMockFunctions:
    def test_mock_deep_analysis(self):
        result = _mock_deep_analysis("AAPL")
        assert result["ticker"] == "AAPL"
        assert result["overall_score"] == 6.5
        assert len(result["dimensions"]) == 8
        assert len(result["risks"]) > 0

    def test_mock_investor_panel(self):
        result = _mock_investor_panel("AAPL")
        assert result["ticker"] == "AAPL"
        assert result["panel_consensus"] == 55.0
        assert result["signal_distribution"]["dominant"] == "bullish"
        assert len(result["investors"]) > 0

    def test_mock_trap_result(self):
        result = _mock_trap_result("AAPL")
        assert result["ticker"] == "AAPL"
        assert result["trap_score"] == 9
        assert result["trap_level"] == "🟢 安全"
        assert result["signals_hit"] == []


# ---------------------------------------------------------------------------
# TestRunUziAnalysis — 主入口
# ---------------------------------------------------------------------------

class TestRunUziAnalysis:
    async def test_unknown_type_yields_error(self):
        store = _mock_store()
        events = await _collect(run_uzi_analysis("AAPL", "bad-type", store, "e1"))
        assert len(events) == 1
        assert events[0]["event"] == "error"
        assert "未知分析类型" in events[0]["message"]
        store.create_uzi_analysis.assert_not_called()

    async def test_deep_analysis_flow(self):
        store = _mock_store()
        mock_result = _mock_deep_analysis("AAPL")

        with patch.object(uzi_runner, "_run_deep_analysis",
                          new_callable=AsyncMock, return_value=mock_result):
            events = await _collect(run_uzi_analysis("AAPL", "deep-analysis", store, "e1"))

        assert events[0]["event"] == "started"
        assert events[0]["analysis_id"] == "test_analysis_id"
        assert events[-1]["event"] == "completed"
        assert events[-1]["result"]["overall_score"] == 6.5
        store.create_uzi_analysis.assert_called_once_with("e1", "AAPL", "deep-analysis")
        store.complete_uzi_analysis.assert_called_once()
        call_kwargs = store.complete_uzi_analysis.call_args
        assert call_kwargs[1]["score"] == 6.5

    async def test_investor_panel_flow(self):
        store = _mock_store()
        mock_result = _mock_investor_panel("AAPL")

        with patch.object(uzi_runner, "_run_investor_panel",
                          new_callable=AsyncMock, return_value=mock_result):
            events = await _collect(run_uzi_analysis("AAPL", "investor-panel", store, "e1"))

        assert events[-1]["event"] == "completed"
        assert events[-1]["result"]["signal_distribution"]["dominant"] == "bullish"
        call_kwargs = store.complete_uzi_analysis.call_args
        assert call_kwargs[1]["signal"] == "bullish"

    async def test_trap_detector_flow(self):
        store = _mock_store()
        mock_result = _mock_trap_result("AAPL")

        with patch.object(uzi_runner, "_run_trap_detector",
                          new_callable=AsyncMock, return_value=mock_result):
            events = await _collect(run_uzi_analysis("AAPL", "trap-detector", store, "e1"))

        assert events[-1]["event"] == "completed"
        call_kwargs = store.complete_uzi_analysis.call_args
        assert call_kwargs[1]["trap_level"] == "🟢 安全"

    async def test_lhb_analyzer_flow(self):
        store = _mock_store()
        mock_result = {"ticker": "SH600519", "records": [{"a": 1}], "count": 1,
                       "summary": "近期共 1 条龙虎榜记录"}

        with patch.object(uzi_runner, "_run_lhb_analyzer",
                          new_callable=AsyncMock, return_value=mock_result):
            events = await _collect(run_uzi_analysis("SH600519", "lhb-analyzer", store, "e1"))

        assert events[-1]["event"] == "completed"
        assert events[-1]["result"]["count"] == 1

    async def test_exception_marks_failed(self):
        store = _mock_store()

        with patch.object(uzi_runner, "_run_deep_analysis",
                          new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            events = await _collect(run_uzi_analysis("AAPL", "deep-analysis", store, "e1"))

        assert events[-1]["event"] == "error"
        assert events[-1]["status"] == "failed"
        assert "boom" in events[-1]["message"]
        store.fail_uzi_analysis.assert_called_once_with("test_analysis_id", "boom")


# ---------------------------------------------------------------------------
# TestDeepAnalysis — _run_deep_analysis
# ---------------------------------------------------------------------------

class TestDeepAnalysis:
    async def test_no_llm_returns_mock(self):
        with patch.object(uzi_runner, "get_llm_for_position", return_value=(None, "", "")):
            result = await uzi_runner._run_deep_analysis("AAPL", [])
        assert result["overall_score"] == 6.5
        assert len(result["dimensions"]) == 8

    async def test_with_llm_runs_8_dims(self):
        mock_llm = MagicMock()
        call_count = 0

        async def mock_to_thread(fn):
            nonlocal call_count
            call_count += 1
            return f"7|维度{call_count}表现良好"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_deep_analysis("AAPL", [])

        assert call_count == 8
        assert result["overall_score"] == 7.0
        assert len(result["dimensions"]) == 8

    async def test_dim_exception_gives_default_score(self):
        """_analyze_dimension 内部捕获异常，返回 (5, "评估中")。"""
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            raise RuntimeError("LLM error")

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_deep_analysis("AAPL", [])

        for d in result["dimensions"].values():
            assert d["score"] == 5


# ---------------------------------------------------------------------------
# TestInvestorPanel — _run_investor_panel
# ---------------------------------------------------------------------------

class TestInvestorPanel:
    async def test_no_llm_returns_mock(self):
        with patch.object(uzi_runner, "get_llm_for_position", return_value=(None, "", "")):
            result = await uzi_runner._run_investor_panel("AAPL", [])
        assert result["panel_consensus"] == 55.0
        assert result["signal_distribution"]["dominant"] == "bullish"

    async def test_with_llm_runs_9_schools(self):
        mock_llm = MagicMock()
        call_count = 0

        async def mock_to_thread(fn):
            nonlocal call_count
            call_count += 1
            return "bullish|75|买入|模拟测试理由"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_investor_panel("AAPL", [])

        assert call_count == 9
        assert result["panel_consensus"] == 100.0
        assert result["signal_distribution"]["bullish"] == 9
        assert len(result["investors"]) == 9

    async def test_investor_exception_gives_neutral(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            raise RuntimeError("boom")

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_investor_panel("AAPL", [])

        for inv in result["investors"]:
            assert inv["signal"] == "neutral"


# ---------------------------------------------------------------------------
# TestLhbAnalyzer — _run_lhb_analyzer
# ---------------------------------------------------------------------------

class TestLhbAnalyzer:
    async def test_with_data(self):
        import pandas as pd
        mock_df = pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})

        async def mock_to_thread(fn):
            return mock_df

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._run_lhb_analyzer("SH600519", [])

        assert result["count"] == 3
        assert len(result["records"]) == 3
        assert "3 条龙虎榜" in result["summary"]

    async def test_akshare_error(self):
        async def mock_to_thread(fn):
            raise RuntimeError("akshare error")

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._run_lhb_analyzer("SH600519", [])

        assert result["count"] == 0
        assert result["records"] == []
        assert "未找到" in result["summary"]

    async def test_empty_dataframe(self):
        import pandas as pd
        mock_df = pd.DataFrame()

        async def mock_to_thread(fn):
            return mock_df

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._run_lhb_analyzer("SH600519", [])

        assert result["count"] == 0
        assert result["records"] == []


# ---------------------------------------------------------------------------
# TestTrapDetector — _run_trap_detector
# ---------------------------------------------------------------------------

class TestTrapDetector:
    async def test_no_llm_returns_mock(self):
        with patch.object(uzi_runner, "get_llm_for_position", return_value=(None, "", "")):
            result = await uzi_runner._run_trap_detector("AAPL", [])
        assert result["trap_level"] == "🟢 安全"
        assert result["trap_score"] == 9
        assert result["signals_hit"] == []

    async def test_with_llm_all_safe(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "否|未发现相关问题"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_trap_detector("AAPL", [])

        assert result["trap_level"] == "🟢 安全"
        assert result["trap_score"] == 9
        assert len(result["signals_hit"]) == 0

    async def test_with_llm_some_hits(self):
        mock_llm = MagicMock()
        call_count = 0

        async def mock_to_thread(fn):
            nonlocal call_count
            call_count += 1
            if call_count <= 4:
                return "是|发现异常行为"
            return "否|正常"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_trap_detector("AAPL", [])

        assert result["trap_level"] == "🟠 警惕"
        assert result["trap_score"] == 4
        assert len(result["signals_hit"]) == 4

    async def test_with_llm_high_risk(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "是|高风险信号"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_trap_detector("AAPL", [])

        assert result["trap_level"] == "🔴 高度可疑"
        assert result["trap_score"] == 2
        assert len(result["signals_hit"]) == 8

    async def test_signal_exception_skipped(self):
        mock_llm = MagicMock()
        call_count = 0

        async def mock_to_thread(fn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM error")
            return "否|正常"

        with patch.object(uzi_runner, "get_llm_for_position", return_value=(mock_llm, "p", "m")), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread), \
             patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.sleep",
                   new_callable=AsyncMock):
            result = await uzi_runner._run_trap_detector("AAPL", [])

        assert result["trap_level"] == "🟢 安全"
        assert len(result["signals_hit"]) == 0


# ---------------------------------------------------------------------------
# TestAnalyzeDimension — _analyze_dimension
# ---------------------------------------------------------------------------

class TestAnalyzeDimension:
    async def test_parses_score_and_comment(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "8|ROE连续高于15%"

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            score, comment = await uzi_runner._analyze_dimension(
                mock_llm, "AAPL", "financials", "财报质量")

        assert score == 8
        assert "ROE" in comment

    async def test_clamps_score(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "15|超高分"

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            score, comment = await uzi_runner._analyze_dimension(
                mock_llm, "AAPL", "financials", "财报质量")

        assert score == 10

    async def test_exception_returns_default(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            raise RuntimeError("LLM down")

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            score, comment = await uzi_runner._analyze_dimension(
                mock_llm, "AAPL", "financials", "财报质量")

        assert score == 5
        assert comment == "评估中"


# ---------------------------------------------------------------------------
# TestEvaluateAsInvestor — _evaluate_as_investor
# ---------------------------------------------------------------------------

class TestEvaluateAsInvestor:
    async def test_parses_full_response(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "bullish|85|买入|基本面优秀，ROE持续增长"

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._evaluate_as_investor(
                mock_llm, "AAPL", "巴菲特", "经典价值", "ROE、护城河")

        assert result["signal"] == "bullish"
        assert result["score"] == 85
        assert result["verdict"] == "买入"
        assert "ROE" in result["reasoning"]
        assert result["name"] == "巴菲特"
        assert result["group"] == "经典价值"

    async def test_invalid_signal_defaults_neutral(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "maybe|60|观望|不确定"

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._evaluate_as_investor(
                mock_llm, "AAPL", "达里奥", "宏观对冲", "利率周期")

        assert result["signal"] == "neutral"

    async def test_malformed_response(self):
        mock_llm = MagicMock()

        async def mock_to_thread(fn):
            return "这只股票不太好判断"

        with patch("bottleneck_hunter.watchlist.uzi_runner.asyncio.to_thread",
                   side_effect=mock_to_thread):
            result = await uzi_runner._evaluate_as_investor(
                mock_llm, "AAPL", "西蒙斯", "量化系统", "因子")

        assert result["signal"] == "neutral"
        assert result["score"] == 50
        assert result["verdict"] == "观望"
