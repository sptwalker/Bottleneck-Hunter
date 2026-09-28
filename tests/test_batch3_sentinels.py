"""Batch 3（P1-4 / P1-6 / P1-8）回归哨兵。

这三处此前在 tests/ 下**零覆盖**：grep `catalyst_bonus` / `evaluate_batch` /
`_merge_supplier` 在新增本文件前都无命中。每例都对应一个"改回去就会静默出错"的行为。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bottleneck_hunter.chain.models import (
    BottleneckReport,
    CatalystEvent,
    CatalystTimeline,
    FinancialSnapshot,
    MarketRegion,
    SupplierInfo,
    SupplierScorecard,
)
from bottleneck_hunter.chain.supplier_eval import AlphaScorer, SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import _merge_supplier

# ---------------------------------------------------------------------------
# P1-4 催化剂加分按置信度加权
# ---------------------------------------------------------------------------

def _scorecard_with_catalyst(urgency: float, confidences: list[float]) -> SupplierScorecard:
    return SupplierScorecard(
        supplier=SupplierInfo(
            name="催化剂测试", ticker="000001.SZ", market=MarketRegion.A_STOCK,
            sector="", description="",
        ),
        bottleneck_node="测试环节",
        market_position=5, customer_validation=5, capacity_status=5,
        financial_health=5, valuation=5, overall_score=5.0,
        catalyst=CatalystTimeline(
            events=[
                CatalystEvent(event_type="capacity", description=f"事件{i}", confidence=c)
                for i, c in enumerate(confidences)
            ],
            urgency_score=urgency,
        ),
    )


class TestCatalystBonusConfidenceWeighted:
    def test_high_confidence_keeps_full_bonus(self):
        """置信度 10 → 不打折，等于旧口径的 urgency/10*2。"""
        sc = _scorecard_with_catalyst(urgency=8.0, confidences=[10.0, 10.0])
        assert AlphaScorer._compute_catalyst_bonus(sc) == pytest.approx(1.6)

    def test_low_confidence_discounts_bonus(self):
        """同样紧迫、但概率只有三成 → 加分必须显著低于置信度满值的那张卡。

        旧实现下这两张卡加分**完全相同**（都只看 urgency），
        等于把一个"下月可能有"的事件当成"下月几乎必然"来定价。
        """
        sure = _scorecard_with_catalyst(urgency=8.0, confidences=[10.0])
        maybe = _scorecard_with_catalyst(urgency=8.0, confidences=[3.0])
        sure_bonus = AlphaScorer._compute_catalyst_bonus(sure)
        maybe_bonus = AlphaScorer._compute_catalyst_bonus(maybe)
        assert maybe_bonus == pytest.approx(sure_bonus * 0.3)

    def test_averages_across_events(self):
        """多个事件取平均置信度。"""
        sc = _scorecard_with_catalyst(urgency=10.0, confidences=[2.0, 8.0])
        assert AlphaScorer._compute_catalyst_bonus(sc) == pytest.approx(1.0)

    def test_no_events_returns_zero(self):
        sc = _scorecard_with_catalyst(urgency=10.0, confidences=[])
        assert AlphaScorer._compute_catalyst_bonus(sc) == 0.0

    def test_no_catalyst_returns_zero(self):
        sc = _scorecard_with_catalyst(urgency=10.0, confidences=[10.0])
        sc.catalyst = None
        assert AlphaScorer._compute_catalyst_bonus(sc) == 0.0


# ---------------------------------------------------------------------------
# P1-6 多源合并：落败源的字段回填而非丢弃
# ---------------------------------------------------------------------------

def _sup(ticker: str, source: str, **kw) -> SupplierInfo:
    return SupplierInfo(
        name=kw.pop("name", f"{source}公司"), ticker=ticker,
        market=MarketRegion.A_STOCK, sector=kw.pop("sector", ""),
        description="", source=source, **kw,
    )


class TestMergeSupplier:
    def test_backfills_missing_market_cap(self):
        """审查点名的场景：LLM 在前但不填市值，akshare 从板块成分股取到了真实市值。

        旧实现按 ticker 先到先得，akshare 的 market_cap 被整个丢弃——
        而市值正是下游可投性筛选取不回来的一项。
        """
        keep = _sup("600001.SH", "llm")           # 无 market_cap
        extra = _sup("600001.SH", "akshare", market_cap=88.5)
        _merge_supplier(keep, extra)
        assert keep.market_cap == 88.5

    def test_does_not_overwrite_existing(self):
        """首源已有的值不被覆写——优先级语义保持。"""
        keep = _sup("600001.SH", "llm", market_cap=11.0)
        extra = _sup("600001.SH", "akshare", market_cap=88.5)
        _merge_supplier(keep, extra)
        assert keep.market_cap == 11.0

    def test_records_both_sources(self):
        keep = _sup("600001.SH", "llm")
        extra = _sup("600001.SH", "chain")
        _merge_supplier(keep, extra)
        assert keep.sources == ["llm", "chain"]
        assert keep.source == "llm"  # 主源不变

    def test_source_recorded_once(self):
        keep = _sup("600001.SH", "llm")
        _merge_supplier(keep, _sup("600001.SH", "llm"))
        assert keep.sources == ["llm"]

    def test_description_not_backfilled(self):
        """description 带源口吻，不参与回填——否则文本会被另一个源覆写。"""
        keep = _sup("600001.SH", "llm", sector="半导体")
        keep.description = "LLM 写的一段论述"
        extra = _sup("600001.SH", "akshare", sector="电子")
        extra.description = "akshare 的简短描述"
        _merge_supplier(keep, extra)
        assert keep.description == "LLM 写的一段论述"
        assert keep.sector == "半导体"

    def test_backfills_empty_sector_and_name_cn(self):
        """sector / name_cn 的"空"是空串而不是 None。

        第一版把两者塞进数值型那张表按 `is None` 判，分支永不可达（纯死代码）：
        akshare 那条路 sector 取不到就是 ""，于是真实行业名永远补不进来。
        """
        keep = _sup("600001.SH", "llm")            # sector="" / name_cn=""
        extra = _sup("600001.SH", "akshare", sector="电子", name_cn="某电子")
        _merge_supplier(keep, extra)
        assert keep.sector == "电子"
        assert keep.name_cn == "某电子"

    def test_key_products_list_filled_when_empty(self):
        keep = _sup("600001.SH", "llm")
        extra = _sup("600001.SH", "akshare")
        extra.key_products = ["产品A"]
        _merge_supplier(keep, extra)
        assert keep.key_products == ["产品A"]


# ---------------------------------------------------------------------------
# P1-8 评估失败：重试 + 失败票不进结果列表
# ---------------------------------------------------------------------------

def _bn() -> BottleneckReport:
    return BottleneckReport(
        node_name="测试环节", node_description="d", layer=1,
        scores=[], overall_score=7.0,
    )


def _make_evaluator(llm) -> SupplierEvaluator:
    return SupplierEvaluator(llm=llm, language="zh")


class TestEvaluateFailureHandling:
    async def test_returns_none_not_zero_card_when_exhausted(self):
        """重试用尽 → 返回 None。

        旧行为是返回一张全 0 的 scorecard：它会照常排序（永远排最后）、
        照常进报告，与"一家真的很差的公司"完全无法区分。
        """
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        ev = _make_evaluator(llm)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("asyncio.sleep", AsyncMock())
            result = await ev.evaluate(_sup("600001.SH", "llm"), _bn())
        assert result is None
        assert ev.fail_reason("600001.SH")  # 原因已留痕，不是空串

    async def test_retries_then_succeeds(self):
        """偶发失败重试后成功 → 正常出分，不因为一次抖动丢掉这家。"""
        import json
        good = MagicMock()
        good.content = json.dumps({
            "market_position": 8, "customer_validation": 7, "capacity_status": 6,
            "financial_health": 7, "valuation": 6,
        })
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=[RuntimeError("flaky"), good])
        ev = _make_evaluator(llm)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("asyncio.sleep", AsyncMock())
            result = await ev.evaluate(_sup("600001.SH", "llm"), _bn())
        assert result is not None
        assert llm.ainvoke.await_count == 2  # 确实重试了一次

    async def test_evaluate_batch_excludes_failures(self):
        """失败票不出现在返回列表里，但记进 failed_suppliers。"""
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        ev = _make_evaluator(llm)
        suppliers = [_sup("600001.SH", "llm", name="甲公司"), _sup("600002.SH", "llm", name="乙公司")]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("asyncio.sleep", AsyncMock())
            cards = await ev.evaluate_batch(suppliers, _bn(), {})
        assert cards == []
        # 两家各自留痕：并发下失败原因不能串台（故是 dict 而非单字段）
        assert {n for n, _ in ev.failed_suppliers} == {"甲公司", "乙公司"}


class TestEvaluateAllReportsFailures:
    async def test_failure_summary_emitted(self):
        """失败汇总经 _on_progress 上报，而不是静默消失。"""
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        ev = _make_evaluator(llm)
        msgs: list[str] = []
        ev._on_progress = AsyncMock(side_effect=lambda m: msgs.append(m))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("asyncio.sleep", AsyncMock())
            cards = await ev.evaluate_all({"测试环节": [_sup("600001.SH", "llm")]}, [_bn()])
        assert cards == []
        assert any("评估失败" in m for m in msgs), msgs


# ---------------------------------------------------------------------------
# P1-8 相关：财务快照注入仍然工作（防止接线改动打破既有数据锚）
# ---------------------------------------------------------------------------

class TestFinancialMapStillApplied:
    async def test_snapshot_reaches_prompt(self):
        """financial_map 的票，其真实数据应进 prompt（P1-7 接线后 CLI 也走这条路）。"""
        import json
        captured: dict = {}

        async def _ainvoke(messages):
            captured["prompt"] = messages[-1].content
            m = MagicMock()
            m.content = json.dumps({
                "market_position": 8, "customer_validation": 7, "capacity_status": 6,
                "financial_health": 7, "valuation": 6,
            })
            return m

        llm = MagicMock()
        llm.ainvoke = _ainvoke
        ev = _make_evaluator(llm)
        snap = FinancialSnapshot(data_source="akshare_ths", revenue_yi=123.4)
        await ev.evaluate(_sup("600001.SH", "llm"), _bn(), financial_snapshot=snap)
        assert "123.4" in captured["prompt"]
