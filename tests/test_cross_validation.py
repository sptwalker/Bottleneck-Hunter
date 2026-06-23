"""Tests for CrossValidator — 多模型交叉验证。"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bottleneck_hunter.chain.cross_validation import CrossValidator
from bottleneck_hunter.chain.models import (
    CrossValidationReport,
    ModelValidation,
    SupplierInfo,
    SupplierScorecard,
)


def _make_scorecard(name="TestCo", ticker="TEST", score=7.0):
    return SupplierScorecard(
        supplier=SupplierInfo(name=name, ticker=ticker, market="a_stock", sector="半导体", description="desc"),
        bottleneck_node="瓶颈A",
        layer=1,
        market_position=7,
        customer_validation=6,
        capacity_status=8,
        financial_health=7,
        valuation=6,
        overall_score=score,
        strengths=["技术领先"],
        weaknesses=["估值偏高"],
    )


def _mock_llm(score=8, reasoning="看好", concerns=None):
    llm = AsyncMock()
    msg = MagicMock()
    msg.content = json.dumps({
        "score": score,
        "reasoning": reasoning,
        "concerns": concerns or [],
    })
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


class TestValidateSupplier:
    @pytest.mark.asyncio
    async def test_high_score(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("model_a", _mock_llm(score=9)), ("model_b", _mock_llm(score=8))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.consensus_score == 8.5
        assert len(report.validations) == 2
        assert "看好" in report.consensus_reasoning

    @pytest.mark.asyncio
    async def test_low_score(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("m1", _mock_llm(score=3, concerns=["风险大"])), ("m2", _mock_llm(score=4, concerns=["估值高"]))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.consensus_score == 3.5
        assert "不看好" in report.consensus_reasoning

    @pytest.mark.asyncio
    async def test_mixed_scores(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("m1", _mock_llm(score=8)), ("m2", _mock_llm(score=5, concerns=["竞争加剧"]))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.consensus_score == 6.5
        assert "分化" in report.consensus_reasoning

    @pytest.mark.asyncio
    async def test_single_model(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("sole_model", _mock_llm(score=7))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.consensus_score == 7.0
        assert len(report.validations) == 1

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self):
        failing_llm = AsyncMock()
        failing_llm.ainvoke = AsyncMock(side_effect=Exception("timeout"))
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("bad_model", failing_llm)]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.consensus_score == 5.0
        assert "失败" in report.validations[0].reasoning

    @pytest.mark.asyncio
    async def test_score_clamped(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("m1", _mock_llm(score=15))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.validations[0].score == 10.0

    @pytest.mark.asyncio
    async def test_score_clamped_low(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("m1", _mock_llm(score=-5))]
        sc = _make_scorecard()
        report = await validator.validate_supplier(sc, llms)
        assert report.validations[0].score == 1.0

    @pytest.mark.asyncio
    async def test_report_fields(self):
        validator = CrossValidator(validation_models=[], language="zh")
        llms = [("m1", _mock_llm(score=7, reasoning="不错", concerns=["竞争"]))]
        sc = _make_scorecard(name="ABC", ticker="ABC.SH")
        report = await validator.validate_supplier(sc, llms)
        assert report.supplier_name == "ABC"
        assert report.ticker == "ABC.SH"
        assert report.avg_score == report.consensus_score


class TestValidateAll:
    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.cross_validation.create_llm")
    async def test_validates_top10(self, mock_create):
        mock_create.return_value = _mock_llm(score=7)
        scorecards = [_make_scorecard(name=f"Co{i}", ticker=f"T{i}", score=max(1, 10-i)) for i in range(15)]
        validator = CrossValidator(
            validation_models=[{"provider": "openai", "model": "gpt"}],
            language="zh",
        )
        reports = await validator.validate_all(scorecards)
        assert len(reports) == 10

    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.cross_validation.create_llm", side_effect=Exception("no key"))
    async def test_no_models_returns_empty(self, mock_create):
        validator = CrossValidator(
            validation_models=[{"provider": "bad", "model": "bad"}],
        )
        reports = await validator.validate_all([_make_scorecard()])
        assert reports == []

    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.cross_validation.create_llm")
    async def test_empty_scorecards(self, mock_create):
        mock_create.return_value = _mock_llm()
        validator = CrossValidator(
            validation_models=[{"provider": "openai", "model": "gpt"}],
        )
        reports = await validator.validate_all([])
        assert reports == []
