"""验收：瓶颈评分接入真实行业集中度(CR3/HHI) + provenance 标注。

对应"把瓶颈评分从纯 LLM 估算升级为 LLM + 真实集中度校验"。
不依赖网络：concentration 计算用纯函数测；prompt 注入与降级用 mock LLM + monkeypatch。
运行：pytest tests/test_industry_concentration.py -q
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from bottleneck_hunter.chain.industry_concentration import (
    _concentration_from_mcaps, _mcap_to_yi, _extract_keywords, compute_concentration,
)
from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.models import ChainGraph, IndustryNode, LayerType


# ── 纯计算逻辑 ──────────────────────────────────────────────
class TestConcentrationMath:
    def test_cr3_hhi_basic(self):
        c = _concentration_from_mcaps([50, 30, 20])
        assert c["cr3"] == 100.0
        assert c["hhi"] == 3800   # 50²+30²+20²
        assert c["company_count"] == 3

    def test_cr5_and_ranking(self):
        c = _concentration_from_mcaps([40, 30, 20, 10])
        assert c["cr3"] == 90.0
        assert c["cr5"] == 100.0
        assert c["hhi"] == 3000

    def test_empty_and_invalid(self):
        assert _concentration_from_mcaps([]) is None
        assert _concentration_from_mcaps([0, -5]) is None

    def test_mcap_unit_conversion(self):
        assert _mcap_to_yi("50000000000") == 500.0   # 元 → 亿
        assert _mcap_to_yi(50) == 50.0               # 已是亿
        assert _mcap_to_yi(None) is None
        assert _mcap_to_yi("--") is None

    def test_keyword_extraction(self):
        assert "光刻胶" in _extract_keywords("高端光刻胶")
        assert _extract_keywords("HBM高带宽内存")  # 非空


# ── 拉取失败降级 ───────────────────────────────────────────
class TestFetchDegradation:
    def test_akshare_import_or_network_fail_returns_none(self):
        # akshare 不可用/网络失败时不抛异常，返回 None
        with patch.dict("sys.modules", {"akshare": None}):
            assert compute_concentration("光刻胶") is None


# ── prompt 注入 + provenance ───────────────────────────────
class _FakeLLM:
    """返回固定 JSON 的假 LLM，并记录收到的 prompt 供断言。"""
    def __init__(self):
        self.last_prompt = ""

    async def ainvoke(self, messages):
        self.last_prompt = messages[-1].content
        class _Resp:
            content = json.dumps({
                "cr3_estimate": 40, "hhi_estimate": 900,  # LLM 的错误估算，应被真实值覆盖
                "scores": [
                    {"dimension": "scarcity", "score": 4, "reasoning": "test"},
                    {"dimension": "irreplaceability", "score": 5, "reasoning": "test"},
                    {"dimension": "supply_demand_gap", "score": 5, "reasoning": "test"},
                    {"dimension": "pricing_power", "score": 4, "reasoning": "test"},
                    {"dimension": "tech_barrier", "score": 6, "reasoning": "test"},
                ],
                "key_insights": ["x"], "risks": ["y"],
            })
        return _Resp()


def _graph():
    root = IndustryNode(name="芯片", description="终端", layer=0,
                        layer_type=LayerType.END_PRODUCT, function="终端产品")
    node = IndustryNode(name="光刻胶", description="感光材料", layer=2,
                        layer_type=LayerType.MATERIAL, function="光刻感光")
    return ChainGraph(sector="半导体", end_product="芯片", nodes=[root, node])


def test_astock_injects_real_concentration_and_overrides():
    """A 股 + 真实数据可得 → prompt 含真实集中度段，report 用真实值且 source=akshare。"""
    fake_llm = _FakeLLM()
    analyzer = BottleneckAnalyzer(llm=fake_llm, market="a_stock")
    real = {"cr3": 78.0, "cr5": 90.0, "hhi": 2600, "company_count": 9,
            "board_name": "光刻胶", "top_companies": [("公司A", 40.0), ("公司B", 25.0)],
            "source": "akshare"}
    with patch("bottleneck_hunter.chain.industry_concentration.compute_concentration",
               return_value=real):
        report = asyncio.run(analyzer._analyze_node("光刻胶", "感光材料", 2, _graph()))

    assert report is not None
    # prompt 注入了真实集中度段
    assert "真实市场集中度数据" in fake_llm.last_prompt
    assert "CR3=78.0%" in fake_llm.last_prompt
    # report 用真实值覆盖 LLM 的 40/900，并标注来源
    assert report.cr3_estimate == 78
    assert report.hhi_estimate == 2600
    assert report.cr3_source == "akshare"
    assert report.concentration_detail["company_count"] == 9


def test_ustock_falls_back_to_llm_estimate():
    """美股 → 不取真实数据，report 用 LLM 估算，source=llm_estimate，行为不变。"""
    fake_llm = _FakeLLM()
    analyzer = BottleneckAnalyzer(llm=fake_llm, market="us_stock")
    # 即便 compute_concentration 可用也不应被调用（market!=a_stock）
    with patch("bottleneck_hunter.chain.industry_concentration.compute_concentration",
               return_value={"cr3": 99}) as m:
        report = asyncio.run(analyzer._analyze_node("GPU", "芯片", 1, _graph()))
    m.assert_not_called()
    assert report.cr3_source == "llm_estimate"
    assert report.cr3_estimate == 40   # LLM 原值，未被覆盖
    assert "真实市场集中度数据" not in fake_llm.last_prompt


def test_astock_fetch_fail_degrades_gracefully():
    """A 股但真实数据拉取失败(None) → 降级 LLM 估算，不报错。"""
    fake_llm = _FakeLLM()
    analyzer = BottleneckAnalyzer(llm=fake_llm, market="a_stock")
    with patch("bottleneck_hunter.chain.industry_concentration.compute_concentration",
               return_value=None):
        report = asyncio.run(analyzer._analyze_node("某冷门环节", "x", 3, _graph()))
    assert report is not None
    assert report.cr3_source == "llm_estimate"
    assert report.cr3_estimate == 40


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
