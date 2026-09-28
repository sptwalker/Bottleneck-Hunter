"""Batch 4（`moat_overall` 幻影零分）回归哨兵。

`moat_overall` 此前用 `data.get(f, 0)` 补齐全部 4 个护城河维度再取均值——
LLM 少答一个维度，就等于给那一项打了 0 分。这是 Batch 2 P1-2 的同一类病
（"缺数据"被当成"值恰好是 0"），只是换了一层。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bottleneck_hunter.chain.models import (
    BottleneckReport,
    MarketRegion,
    SupplierInfo,
)
from bottleneck_hunter.chain.supplier_eval import SupplierEvaluator


def _sup() -> SupplierInfo:
    return SupplierInfo(
        name="护城河测试", ticker="600001.SH",
        market=MarketRegion.A_STOCK, sector="", description="", source="llm",
    )


def _bn() -> BottleneckReport:
    return BottleneckReport(
        node_name="测试环节", node_description="d", layer=1,
        scores=[], overall_score=7.0,
    )


def _llm_returning(payload: dict) -> MagicMock:
    m = MagicMock()
    m.content = json.dumps(payload)
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=m)
    return llm


CORE = {
    "market_position": 7, "customer_validation": 6, "capacity_status": 6,
    "financial_health": 7, "valuation": 6,
}


class TestMoatOverallMissingFields:
    async def test_partial_response_averages_only_answered_dims(self):
        """LLM 只答了 2 个护城河维度 → 取这两项的均值，而不是把缺的两项当 0。

        旧实现下这里得 3.8（=(8+7+0+0)/4），真实均值是 7.5 —— 一次部分响应
        就把护城河从"强"砸到"弱"，并透过 overall = base*0.8 + moat*0.2 传导出去。
        """
        llm = _llm_returning({**CORE, "patent_moat": 8, "switching_cost": 7})
        sc = await SupplierEvaluator(llm=llm, language="zh").evaluate(_sup(), _bn())
        assert sc is not None
        assert sc.moat.overall_moat == pytest.approx(7.5)

    async def test_full_response_unchanged(self):
        """四维齐全时与旧口径逐字节一致——修复不得改变正常路径。"""
        llm = _llm_returning({
            **CORE,
            "patent_moat": 8, "switching_cost": 7,
            "capacity_lead_time": 6, "cost_advantage": 5,
        })
        sc = await SupplierEvaluator(llm=llm, language="zh").evaluate(_sup(), _bn())
        assert sc.moat.overall_moat == pytest.approx(6.5)

    async def test_no_moat_fields_keeps_zero_fallback(self):
        """一个护城河维度都没答 → 维持 0，overall 不加权护城河项（既有回退语义）。"""
        llm = _llm_returning(dict(CORE))
        sc = await SupplierEvaluator(llm=llm, language="zh").evaluate(_sup(), _bn())
        assert sc.moat.overall_moat == 0
