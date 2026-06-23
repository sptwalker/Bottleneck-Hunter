"""Tests for CatalystAnalyzer — 催化剂时间线分析。"""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bottleneck_hunter.chain.catalyst import CatalystAnalyzer, _extract_report_keywords
from bottleneck_hunter.chain.models import (
    BottleneckReport,
    BottleneckScore,
    CatalystTimeline,
    FinancialSnapshot,
    SupplierInfo,
    SupplierScorecard,
)


def _make_supplier(name="TestCo", ticker="TEST"):
    return SupplierInfo(name=name, ticker=ticker, market="a_stock", sector="半导体", description="desc")


def _make_bottleneck(name="瓶颈A"):
    return BottleneckReport(
        node_name=name,
        node_description="关键瓶颈",
        layer=1,
        scores=[
            BottleneckScore(dimension="scarcity", score=8, reasoning="供应商少"),
            BottleneckScore(dimension="irreplaceability", score=7, reasoning="难替代"),
        ],
        overall_score=7.6,
        key_insights=["供需紧张", "技术壁垒高"],
    )


def _make_scorecard(name="TestCo", ticker="TEST"):
    return SupplierScorecard(
        supplier=_make_supplier(name, ticker),
        bottleneck_node="瓶颈A",
        layer=1,
        market_position=7, customer_validation=6, capacity_status=8,
        financial_health=7, valuation=6, overall_score=7.0,
        strengths=["技术领先"], weaknesses=["估值偏高"],
    )


def _mock_llm(response_data: dict):
    llm = AsyncMock()
    msg = MagicMock()
    msg.content = json.dumps(response_data, ensure_ascii=False)
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


VALID_RESPONSE = {
    "events": [
        {"event_type": "capacity", "description": "新产线投产", "expected_date": "2025Q3", "confidence": 8, "impact_score": 7},
        {"event_type": "order", "description": "大客户订单", "expected_date": "2025Q2", "confidence": 6, "impact_score": 8},
    ],
    "urgency_score": 7.5,
    "investment_window": "未来1-2个季度",
    "summary": "催化剂密集期",
}


class TestExtractReportKeywords:
    def test_with_full_snapshot(self):
        snap = FinancialSnapshot(
            data_source="test",
            analyst_report_count=12,
            analyst_rating="买入",
            consensus_eps=2.5,
        )
        result = _extract_report_keywords(snap)
        assert "12" in result
        assert "买入" in result
        assert "2.5" in result

    def test_with_none(self):
        assert _extract_report_keywords(None) == ""

    def test_with_empty_snapshot(self):
        snap = FinancialSnapshot(data_source="test")
        assert _extract_report_keywords(snap) == ""


class TestCatalystAnalyze:
    @pytest.mark.asyncio
    async def test_successful_analysis(self):
        llm = _mock_llm(VALID_RESPONSE)
        analyzer = CatalystAnalyzer(llm=llm, language="zh")
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck())
        assert len(result.events) == 2
        assert result.events[0].event_type == "capacity"
        assert result.urgency_score == 7.5
        assert result.investment_window == "未来1-2个季度"

    @pytest.mark.asyncio
    async def test_score_clamped(self):
        data = {**VALID_RESPONSE, "urgency_score": 15}
        data["events"] = [{"event_type": "t", "description": "d", "expected_date": "Q1", "confidence": 20, "impact_score": -5}]
        llm = _mock_llm(data)
        analyzer = CatalystAnalyzer(llm=llm)
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck())
        assert result.urgency_score == 10.0
        assert result.events[0].confidence == 10.0
        assert result.events[0].impact_score == 0.0

    @pytest.mark.asyncio
    async def test_with_financial_snapshot(self):
        llm = _mock_llm(VALID_RESPONSE)
        analyzer = CatalystAnalyzer(llm=llm)
        snap = FinancialSnapshot(data_source="test", analyst_report_count=5, analyst_rating="增持")
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck(), financial_snapshot=snap)
        assert result.summary == "催化剂密集期"

    @pytest.mark.asyncio
    async def test_timeout_returns_default(self):
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())
        analyzer = CatalystAnalyzer(llm=llm)
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck())
        assert "超时" in result.summary
        assert len(result.events) == 0

    @pytest.mark.asyncio
    async def test_json_error_returns_default(self):
        llm = AsyncMock()
        msg = MagicMock()
        msg.content = "not json at all"
        llm.ainvoke = AsyncMock(return_value=msg)
        analyzer = CatalystAnalyzer(llm=llm)
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck())
        assert "失败" in result.summary or "Error" in result.summary or "error" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("unknown"))
        analyzer = CatalystAnalyzer(llm=llm)
        result = await analyzer.analyze(_make_supplier(), _make_bottleneck())
        assert "RuntimeError" in result.summary


class TestCatalystAnalyzeBatch:
    @pytest.mark.asyncio
    async def test_batch_attaches_to_scorecards(self):
        llm = _mock_llm(VALID_RESPONSE)
        analyzer = CatalystAnalyzer(llm=llm)
        sc = _make_scorecard()
        bottleneck_map = {"瓶颈A": _make_bottleneck()}
        result = await analyzer.analyze_batch([sc], bottleneck_map)
        assert len(result) == 1
        assert result[0].catalyst is not None
        assert result[0].catalyst.urgency_score == 7.5

    @pytest.mark.asyncio
    async def test_batch_missing_bottleneck(self):
        llm = _mock_llm(VALID_RESPONSE)
        analyzer = CatalystAnalyzer(llm=llm)
        sc = _make_scorecard()
        result = await analyzer.analyze_batch([sc], {})
        assert len(result) == 1
