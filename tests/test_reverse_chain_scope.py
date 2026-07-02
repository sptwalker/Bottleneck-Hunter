"""验收：反向分析按所属产业链定位 —— 同一企业在不同赛道得到不同结果。

修复前：_match_existing_bottleneck 全局搜第一个命中、LLM 兜底只看公司自身，
导致同一企业（如 MSFT）跨链得到几乎相同的瓶颈节点/分数。
修复后：优先在 owner 链内匹配；LLM 兜底注入当前产业链上下文做链内定位。
运行：pytest tests/test_reverse_chain_scope.py -q
"""
from __future__ import annotations

import asyncio

from bottleneck_hunter.web.streaming.reverse import (
    _match_existing_bottleneck, _load_chain_context, _llm_identify_bottleneck,
)


class _FakeAnalysisStore:
    """假 AnalysisStore：records = {id: result_json_dict}。"""
    def __init__(self, records: dict):
        self._records = records

    def list_all(self):
        return [{"id": k} for k in self._records]

    def get(self, aid):
        rj = self._records.get(aid)
        return {"id": aid, "result_json": rj, "sector": rj.get("sector", "")} if rj else None


def _rec(sector, node_name, node_score, ticker):
    """构造一条正向记录：含一个瓶颈节点 + 一个已评估该 ticker 的供应商。"""
    return {
        "sector": sector,
        "bottleneck_reports": [{
            "node_name": node_name, "node_description": f"{node_name}环节",
            "layer": 2, "overall_score": node_score, "scores": [],
        }],
        "supplier_scorecards": [{
            "supplier": {"ticker": ticker}, "bottleneck_node": node_name,
        }],
    }


def test_match_prioritizes_owner_chain():
    """同一企业出现在两条链里，应优先返回 owner 链的那个瓶颈节点。"""
    store = _FakeAnalysisStore({
        "chainA": _rec("GPU/AI算力", "HBM存储接口", 8.5, "MSFT"),
        "chainB": _rec("办公软件", "企业云服务", 6.0, "MSFT"),
    })
    # owner=chainB → 拿到办公软件链的节点，而非全局第一个(chainA)
    rep, mid = _match_existing_bottleneck(store, "MSFT", owner_analysis_id="chainB")
    assert mid == "chainB"
    assert rep.node_name == "企业云服务"
    assert rep.overall_score == 6.0
    # owner=chainA → 拿到 GPU 链的节点
    rep2, mid2 = _match_existing_bottleneck(store, "MSFT", owner_analysis_id="chainA")
    assert mid2 == "chainA"
    assert rep2.node_name == "HBM存储接口"
    # 关键：同一企业跨链得到【不同】节点与分数
    assert rep.node_name != rep2.node_name
    assert rep.overall_score != rep2.overall_score


def test_match_falls_back_to_global_when_not_in_owner_chain():
    """owner 链里没有该企业时，回退全局搜索仍能复用已有瓶颈分。"""
    store = _FakeAnalysisStore({
        "chainA": _rec("GPU/AI算力", "HBM存储接口", 8.5, "NVDA"),
        "chainB": _rec("办公软件", "企业云服务", 6.0, "MSFT"),
    })
    # owner=chainA 里没有 MSFT → 回退到 chainB
    rep, mid = _match_existing_bottleneck(store, "MSFT", owner_analysis_id="chainA")
    assert mid == "chainB"
    assert rep.node_name == "企业云服务"


def test_no_owner_still_works():
    """不传 owner（旧行为）时全局搜索，兼容。"""
    store = _FakeAnalysisStore({"chainA": _rec("GPU/AI算力", "HBM存储接口", 8.5, "NVDA")})
    rep, mid = _match_existing_bottleneck(store, "NVDA")
    assert mid == "chainA" and rep.node_name == "HBM存储接口"


def test_load_chain_context():
    """_load_chain_context 返回赛道 + 按瓶颈分降序的节点清单。"""
    store = _FakeAnalysisStore({
        "chainA": {
            "sector": "GPU/AI算力",
            "bottleneck_reports": [
                {"node_name": "光模块", "overall_score": 7.0, "layer": 1},
                {"node_name": "HBM存储接口", "overall_score": 8.5, "layer": 1},
            ],
        },
    })
    ctx = _load_chain_context(store, "chainA")
    assert ctx["sector"] == "GPU/AI算力"
    # 降序：HBM(8.5) 在前
    assert ctx["nodes"][0]["name"] == "HBM存储接口"
    assert [n["name"] for n in ctx["nodes"]] == ["HBM存储接口", "光模块"]
    # 无 owner → None
    assert _load_chain_context(store, "") is None


class _FakeLLM:
    """假 LLM：识别调用返回 identify-JSON 并记录 prompt；打分调用返回 scores-JSON。"""
    def __init__(self):
        self.last_prompt = ""

    async def ainvoke(self, prompt):
        # BottleneckAnalyzer 打分用 messages(list)，识别用 str prompt
        if isinstance(prompt, list):
            content = ('{"cr3_estimate":50,"hhi_estimate":1200,"scores":['
                       '{"dimension":"scarcity","score":5,"reasoning":"t"},'
                       '{"dimension":"irreplaceability","score":5,"reasoning":"t"},'
                       '{"dimension":"supply_demand_gap","score":5,"reasoning":"t"},'
                       '{"dimension":"pricing_power","score":5,"reasoning":"t"},'
                       '{"dimension":"tech_barrier","score":5,"reasoning":"t"}],'
                       '"key_insights":["i"],"risks":["r"]}')
        else:
            self.last_prompt = prompt
            content = ('{"sector":"GPU/AI算力","end_product":"GPU","node_name":"企业云服务",'
                       '"node_description":"x","layer":1,"layer_type":"component",'
                       '"function":"云算力","key_parameters":["p"]}')

        class _R:
            pass
        r = _R()
        r.content = content
        return r


def test_llm_fallback_injects_chain_context():
    """LLM 兜底判定时，当前产业链上下文（赛道+环节清单）被写进 prompt。"""
    fake = _FakeLLM()
    ctx = {"sector": "GPU/AI算力", "nodes": [
        {"name": "HBM存储接口", "score": 8.5, "layer": 1},
        {"name": "光模块", "score": 7.0, "layer": 1},
    ]}
    rep = asyncio.run(_llm_identify_bottleneck(
        fake, {"name": "微软", "sector": "科技"}, "MSFT", "us_stock", None, "zh",
        chain_context=ctx))
    assert rep is not None
    # prompt 含当前链赛道与环节清单 + 链内定位指令
    assert "当前分析的产业链：GPU/AI算力" in fake.last_prompt
    assert "HBM存储接口" in fake.last_prompt
    assert "优先" in fake.last_prompt and "这条产业链" in fake.last_prompt


def test_llm_fallback_without_context_unchanged():
    """无链上下文时不注入（旧行为），prompt 不含链内定位段。"""
    fake = _FakeLLM()
    asyncio.run(_llm_identify_bottleneck(
        fake, {"name": "微软"}, "MSFT", "us_stock", None, "zh", chain_context=None))
    assert "当前分析的产业链" not in fake.last_prompt


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
