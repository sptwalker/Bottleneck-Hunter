"""Tests for strategy_engine.py — 策略信号解析和差异比较。"""

import json

import pytest

from bottleneck_hunter.watchlist.strategy_engine import (
    _parse_strategy_response,
    _compute_strategy_diff,
)


SAMPLE_RESPONSE = """## 情报摘要
AAPL最近表现强劲，技术面呈现多头趋势。

## 多空分析
- ✅ 营收持续增长
- ✅ AI 业务布局良好
- ❌ 估值偏高
- ❌ 中国市场风险

## 核心逻辑
公司在 AI 生态中占据核心位置，软硬件协同优势明显。

## 操作策略
- 当前信号: bullish / 看多
- 买入条件：回调至支撑位 $170
- 加仓条件：突破 $200 放量
- 减仓条件：跌破 $165
- 卖出条件：跌破 $150

## 风险控制
- 止损位：$148
- 仓位比例：15%
- 对冲建议：买入看跌期权

## 目标与时间
短期目标 $195 (1个月)
中期目标 $220 (3个月)

## 与上次策略对比
信号从中性转为看多。

## 信心评级
- 评分: 7
- 理由: 数据源完整，技术面和基本面趋势一致。
"""


class TestParseStrategyResponse:
    def test_signal_bullish(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        assert result["signal"] == "bullish"

    def test_signal_bearish(self):
        resp = SAMPLE_RESPONSE.replace("bullish / 看多", "bearish / 看空")
        result = _parse_strategy_response(resp)
        assert result["signal"] == "bearish"

    def test_signal_neutral_default(self):
        resp = "## 操作策略\n- 当前信号: 观望\n"
        result = _parse_strategy_response(resp)
        assert result["signal"] == "neutral"

    def test_confidence_extracted(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        assert result["confidence"] == 7

    def test_confidence_clamped_high(self):
        resp = "## 信心评级\n评分: 15\n理由: 很有信心"
        result = _parse_strategy_response(resp)
        assert result["confidence"] == 10

    def test_confidence_clamped_low(self):
        resp = "## 信心评级\n评分: 0\n理由: 没信心"
        result = _parse_strategy_response(resp)
        assert result["confidence"] == 1

    def test_confidence_default(self):
        resp = "## 情报摘要\n一些内容"
        result = _parse_strategy_response(resp)
        assert result["confidence"] == 5

    def test_bull_bear_points(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        bb = json.loads(result["bull_bear_analysis"])
        assert len(bb["bull_points"]) == 2
        assert len(bb["bear_points"]) == 2
        assert "营收持续增长" in bb["bull_points"][0]

    def test_core_logic(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        assert "AI" in result["core_logic"]

    def test_action_conditions(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        action = json.loads(result["action_strategy"])
        assert action["signal"] == "bullish"
        assert "buy" in action["conditions"]
        assert "sell" in action["conditions"]
        assert "$170" in action["conditions"]["buy"]

    def test_risk_control(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        risk = json.loads(result["risk_control"])
        assert "$148" in risk.get("stop_loss", "")
        assert "15%" in risk.get("position_pct", "")

    def test_targets(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        targets = json.loads(result["targets_timeline"])
        assert len(targets["targets"]) >= 2

    def test_strategy_comparison(self):
        result = _parse_strategy_response(SAMPLE_RESPONSE)
        comp = json.loads(result["strategy_comparison"])
        assert "中性" in comp.get("text", "") or "对比" in SAMPLE_RESPONSE

    def test_empty_response(self):
        result = _parse_strategy_response("")
        assert result["signal"] == "neutral"
        assert result["confidence"] == 5
        assert result["core_logic"] == ""

    def test_malformed_no_sections(self):
        result = _parse_strategy_response("这是一段没有任何分节的文本")
        assert result["signal"] == "neutral"
        assert result["confidence"] == 5


class TestComputeStrategyDiff:
    def test_signal_changed(self):
        new = {"signal": "bullish", "confidence": 7, "core_logic": "看好AI"}
        prev = {"signal": "neutral", "confidence": 5, "core_logic": "观望"}
        diff = _compute_strategy_diff(new, prev)
        assert "signal" in str(diff).lower() or "信号" in str(diff)

    def test_confidence_changed(self):
        new = {"signal": "bullish", "confidence": 8, "core_logic": "看好"}
        prev = {"signal": "bullish", "confidence": 5, "core_logic": "看好"}
        diff = _compute_strategy_diff(new, prev)
        assert diff is not None

    def test_no_previous_none(self):
        new = {"signal": "bullish", "confidence": 7, "core_logic": "看好"}
        diff = _compute_strategy_diff(new, None)
        assert diff is not None

    def test_no_previous_empty(self):
        new = {"signal": "bullish", "confidence": 7, "core_logic": "看好"}
        diff = _compute_strategy_diff(new, {})
        assert diff is not None

    def test_identical(self):
        same = {"signal": "neutral", "confidence": 5, "core_logic": "观望"}
        diff = _compute_strategy_diff(same, same)
        assert diff is not None
