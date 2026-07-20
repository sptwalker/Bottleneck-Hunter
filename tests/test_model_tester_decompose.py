"""chain_decompose 维度打分自检 —— 宽/窄/坏三种拆解结果的分数符合预期，且 scorer 不抛异常。

打分逻辑针对根因：记录 #10(deepseek 457节点) vs #13(minimax 79节点) 的广度差，
必须让"每节点拆得多"的模型拿高分、"拆得少/拆不动"的拿低分。
另含错误捕获自检：0 分必须能区分「欠费/超时/连不通」vs「真拆不动」，不留空 error。
"""
import asyncio

import bottleneck_hunter.web.model_tester as mt
from bottleneck_hunter.web.model_tester import _score_chain_decompose
from bottleneck_hunter.web.model_tester import test_chain_decompose as run_chain_decompose  # 别名：避免 pytest 误采集为用例


def _node(name, code="002371"):
    return {
        "name": name, "description": "d", "function": "f",
        "upstream_deps": ["x"], "dependency": 0.8,
        "representative_companies": [{"name": "某公司", "code": code}],
    }


def test_wide_decompose_scores_high():
    # 每层 8 个结构完整的子节点，孙节点为真上游、不重名 → 接近满分
    layer1 = [_node(f"子{i}") for i in range(8)]
    layer2 = [_node(f"孙{i}") for i in range(8)]
    score, bd = _score_chain_decompose(layer1, layer2, "HBM", "子0")
    assert bd["breadth"] == 4.0, bd            # avg 8 → 满广度
    assert bd["structure"] == 3.0, bd          # 全字段齐全
    assert bd["dedup"] == 1.0, bd              # 无重叠
    assert score >= 9.0, (score, bd)


def test_narrow_decompose_scores_low():
    # 每层 2 个 → 广度显著低（复现 minimax 那种"惜字"模型）
    layer1 = [_node("子0"), _node("子1")]
    layer2 = [_node("孙0"), _node("孙1")]
    score_narrow, bd = _score_chain_decompose(layer1, layer2, "HBM", "子0")
    assert bd["breadth"] == 1.0, bd            # avg 2 → 2/8*4=1.0

    wide, _ = _score_chain_decompose(
        [_node(f"子{i}") for i in range(8)],
        [_node(f"孙{i}") for i in range(8)], "HBM", "子0")
    assert wide - score_narrow >= 2.5, (wide, score_narrow)  # 宽的显著高于窄的


def test_empty_and_bad_input_zero_and_safe():
    # 空结果 → 0 分且不抛
    score, bd = _score_chain_decompose([], [], "HBM", "")
    assert score == 0.0, (score, bd)
    # 非 list / 脏元素 → 视为空，不抛异常
    score2, _ = _score_chain_decompose(None, "not-a-list", "HBM", "")
    assert score2 == 0.0
    score3, bd3 = _score_chain_decompose([{"garbage": 1}, "str"], [], "HBM", "")
    assert bd3["structure"] == 0.0  # 无一元素合规
    assert score3 >= 0.0


def test_lazy_repeat_parent_penalized_on_dedup():
    # 孙节点全部复读子节点名 → 去重分为 0
    layer1 = [_node("A"), _node("B")]
    layer2 = [_node("A"), _node("B")]
    _, bd = _score_chain_decompose(layer1, layer2, "HBM", "A")
    assert bd["dedup"] == 0.0, bd


# ── 错误捕获自检（deepseek 欠费 → 0 分那次排查暴露的问题）────────────

class _BalanceError(Exception):
    """模拟 provider SDK 抛出但 str() 为空的异常（如某些 402/连接异常）。"""
    def __str__(self):
        return ""


def test_exception_branch_fills_type_name_and_reason(monkeypatch):
    """create_llm 直接抛异常且 str(e) 为空 → error 必须带类型名、fail_reason 有中文归因。"""
    def _boom(*a, **k):
        raise _BalanceError()
    monkeypatch.setattr(mt, "create_llm", _boom)

    r = asyncio.run(run_chain_decompose("deepseek", "deepseek-chat"))
    assert r["score"] == 0.0
    assert r["error"] == "_BalanceError"          # str 为空 → 退回类型名，绝不留空
    assert r.get("fail_reason"), r               # classify_reason 给了中文短语


def test_zero_nodes_surfaces_decompose_fail_reason(monkeypatch):
    """连上但 LLM 拒绝（欠费/限流）→ _decompose_layer 返回 [] 且记 _last_fail_reason → 必须带出。"""
    from bottleneck_hunter.chain import decomposer as dmod

    class _FakeLLM:  # create_llm 返回它，避免真实网络
        pass

    monkeypatch.setattr(mt, "create_llm", lambda *a, **k: _FakeLLM())

    async def _fake_layer(self, end_product, parent, depth, existing=None):
        self._last_fail_reason = "频率限制/额度不足"   # 模拟欠费降级
        return []
    monkeypatch.setattr(dmod.ChainDecomposer, "_decompose_layer", _fake_layer)

    r = asyncio.run(run_chain_decompose("deepseek", "deepseek-chat"))
    assert r["score"] == 0.0
    assert r["layer1_count"] == 0
    assert r["fail_reason"] == "频率限制/额度不足", r     # 区分「欠费」而非含糊 0 分
    assert "额度不足" in r["error"], r

