"""Batch 5C 哨兵：情报面板的源评分卡一直是空的（假闭环）。

生产实证（2026-09-28，`analyses.db`，375 张 scorecard）：

```
顶层含 quality_score/alpha_score/final_score 的: 0
在 final.* 下齐全的:                            375
```

消费方 `_aggregate_source_scorecard` 读的是**顶层**，于是每次都返回
`{"quality_score": None, "alpha_score": None, "final_score": None}` ——
`json.dumps` 把 None 写成 `null` 不报错，LLM 简报 prompt 里那行「供应链评分」
长期是三个 null，面板上也一直空着。**没有任何异常，所以没有任何人发现。**

本文件盯住：那三个分必须从**生产者实际写入的位置**取到。
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from bottleneck_hunter.watchlist import strategy_engine as se


def _scorecard(**overrides):
    """生产形状：分数只在 `final.*` 下，顶层无这三个键。"""
    sc = {
        "supplier": {"ticker": "SQM", "name": "SQM"},
        "bottleneck_node": "锂盐",
        "overall_score": 6.0,
        "final": {"quality_score": 6.0, "alpha_score": 6.6, "final_score": 6.35},
        "alpha": {"alpha_score": 6.6},
    }
    sc.update(overrides)
    return sc


class _FakeStore:
    def __init__(self, payload):
        self._payload = payload

    def get(self, _id):
        return {"result_json": json.dumps(self._payload)}


def _run(cards, ticker="SQM"):
    payload = {"supplier_scorecards": cards}
    with patch("bottleneck_hunter.dataflows.store.AnalysisStore",
               return_value=_FakeStore(payload)):
        import asyncio
        return asyncio.run(se._aggregate_source_scorecard(
            "entry1", {"source": "phase4", "source_analysis_id": "a1", "ticker": ticker}))


def test_scores_are_read_from_where_the_producer_writes_them():
    """哨兵本体：生产形状（分数只在 final.*）下必须取到真数，不是三个 null。

    修复前必然失败 —— 顶层没有这三个键，`sc.get(...)` 全返回 None，
    而整条链路静默。
    """
    got = _run([_scorecard()])
    assert got["quality_score"] == 6.0, "quality_score 仍未从 final.* 取到"
    assert got["alpha_score"] == 6.6, "alpha_score 仍未从 final.* 取到"
    assert got["final_score"] == 6.35, "final_score 仍未从 final.* 取到"
    # 没有一个是 None —— 这正是修复前每次都发生的事
    assert all(got[k] is not None for k in ("quality_score", "alpha_score", "final_score"))
    assert got["overall_score"] == 6.0
    assert got["bottleneck_node"] == "锂盐"


def test_alpha_falls_back_to_alpha_block_when_final_is_missing():
    """`final` 缺失时 alpha 退到 `alpha.alpha_score`；quality 退到 overall_score。

    老记录（`final` 字段上线前存的）不该因为缺 final 就整块变 null。
    """
    legacy = {
        "supplier": {"ticker": "AAA"},
        "overall_score": 7.2,
        "alpha": {"alpha_score": 8.1},
    }
    got = _run([legacy], ticker="AAA")
    assert got["quality_score"] == 7.2      # 退到 overall_score
    assert got["alpha_score"] == 8.1        # 退到 alpha.alpha_score
    assert got["final_score"] is None       # 确实没这个数，如实为 None


def test_no_alpha_block_at_all_stays_none_not_fabricated():
    """两个来源都没有就是 None —— 不得回退到中性分 5.0 把「不知道」装成「中等」。"""
    got = _run([{"supplier": {"ticker": "BBB"}, "overall_score": 6.0}], ticker="BBB")
    assert got["alpha_score"] is None
    assert got["final_score"] is None


def test_ticker_mismatch_returns_empty():
    """点错股票不能拿到别人的评分卡。"""
    assert _run([_scorecard()], ticker="NVDA") == {}


def test_non_phase4_entry_returns_empty():
    import asyncio
    assert asyncio.run(se._aggregate_source_scorecard(
        "e", {"source": "manual", "ticker": "SQM"})) == {}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
