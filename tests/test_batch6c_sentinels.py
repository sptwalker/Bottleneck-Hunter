"""Batch 6C 哨兵：候选来源（`SupplierInfo.sources`）必须**被消费**。

检索合并时已按 ticker 留痕 `sources`，但全仓没有一个读取方：
- `search()` 末尾 `[:max_results]` 按合并顺序（LLM 优先）截断，
  10 家 LLM 自报会挤掉 akshare/gangtise 也命中的票；
- 评估 prompt 看不到来源，无法区分「LLM 说它属于该环节」与「板块成分股里真有它」。
"""
from __future__ import annotations

import asyncio

from bottleneck_hunter.chain.models import BottleneckReport, MarketRegion, SupplierInfo
from bottleneck_hunter.chain.supplier_search import SupplierSearcher


def _bn():
    return BottleneckReport(node_name="光刻胶", node_description="d", layer=2, scores=[], overall_score=7.0)


def _s(t, src):
    return SupplierInfo(name=t, ticker=t, market=MarketRegion.A_STOCK, sector="", description="", source=src)


def _search(monkeypatch, llm, akshare, chain=()):
    import bottleneck_hunter.chain.supplier_search as ss
    import bottleneck_hunter.data_provider.hub as hub

    class _NoHub:
        async def fetch(self, *a):
            return {}
    monkeypatch.setattr(hub, "get_hub", lambda: _NoHub())  # gangtise 不碰网络
    monkeypatch.setattr(ss, "_try_akshare_search", lambda *a: [_s(t, "akshare") for t in akshare])

    eng = SupplierSearcher(market=MarketRegion.A_STOCK, max_results=3)
    eng.llm = object()

    async def _llm(_bn):
        return [_s(t, "llm") for t in llm]

    async def _chain(*_a):
        return [_s(t, "chain") for t in chain]
    eng._llm_recommend = _llm
    eng._extract_chain_candidates = _chain
    return asyncio.run(eng.search(_bn(), keywords=["光刻胶"], chain_graph=object() if chain else None))


class TestTruncationPrefersExternallyVerified:
    def test_cross_hit_survives_llm_flood(self, monkeypatch):
        """LLM 自报 5 家 + 其中 1 家也在 akshare 成分股里 + 1 家仅 akshare；
        max_results=3。双源那家必须留下 —— 修复前它排第 5 被截掉。"""
        out = _search(monkeypatch, ["L1", "L2", "L3", "L4", "X"], ["X", "A1"])
        tickers = [s.ticker for s in out]
        assert "X" in tickers
        assert tickers[0] == "X"  # 外部核对 + 双源 排最前
        assert "A1" in tickers    # 仅 akshare 也胜过纯 LLM 自报

    def test_llm_plus_chain_is_not_external(self, monkeypatch):
        """chain 也是拆解阶段 LLM 自报 —— llm+chain 双命中**不能**冒充外部核对，
        不得排到仅 akshare 的票前面。"""
        out = _search(monkeypatch, ["L1", "Y"], ["A1"], chain=["Y"])
        tickers = [s.ticker for s in out]
        assert tickers.index("A1") < tickers.index("Y")

    def test_order_within_tier_is_stable(self, monkeypatch):
        """无外部源时维持原优先级（LLM 顺序），不打乱。"""
        out = _search(monkeypatch, ["L1", "L2", "L3", "L4"], [])
        assert [s.ticker for s in out] == ["L1", "L2", "L3"]


class TestSourceReachesEvalPrompt:
    def _prompt(self, supplier):
        from unittest.mock import AsyncMock, MagicMock

        from bottleneck_hunter.chain.supplier_eval import SupplierEvaluator
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=RuntimeError("stop"))
        ev = SupplierEvaluator(llm=llm)
        ev.MAX_RETRIES = 0
        asyncio.run(ev.evaluate(supplier, _bn()))
        return llm.ainvoke.call_args[0][0][-1].content

    def test_llm_only_is_flagged(self):
        s = _s("L1", "llm")
        s.sources = ["llm"]
        assert "环节归属未经外部数据核对" in self._prompt(s)

    def test_external_hit_is_flagged(self):
        s = _s("X", "llm")
        s.sources = ["llm", "akshare"]
        p = self._prompt(s)
        assert "候选来源: llm+akshare" in p and "含外部板块/选股数据核对" in p

    def test_manual_ticker_gets_no_label(self):
        """用户手动输入的票 sources 为空 —— 不能被误标成「LLM 自报」。"""
        assert "候选来源" not in self._prompt(_s("MANUAL", "llm"))
