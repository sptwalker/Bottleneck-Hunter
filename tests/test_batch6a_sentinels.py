"""Batch 6A 哨兵：链节点的「约束类字段」—— 采集 + **真的被消费**。

审计 P2-1：`IndustryNode` 只能装下「叫什么、干什么、谁在做」，装不下
「这个环节会不会被卡脖子」的直接判据（供应结构 / 扩产周期 / 认证周期 /
地理集中度 / 出口管制风险）。这些事实在拆解阶段本来就知道，丢掉之后
下游分析层只能从 0 重猜。

本文件盯两件事，第二件才是关键：
1. 采集侧 —— LLM 的写法（`"独家"` / `0` / `"unknown"`）要折叠成枚举或 None；
2. **消费侧** —— 采到的字段必须真的进入打分 prompt。只加字段不接线，
   就是又造一个假闭环：字段有了、没人用，而且全程无异常。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.decomposer import (
    ChainDecomposer,
    _safe_int_or_none,
    _safe_risk,
    _safe_str_or_none,
    _safe_structure,
)
from bottleneck_hunter.chain.models import ChainGraph, ChainLink, IndustryNode, LayerType


# ── 采集侧：归一 ────────────────────────────────────────────
class TestNormalizers:
    def test_months_zero_means_unknown_not_zero(self):
        """扩产/认证周期不可能是 0 —— LLM 写 0 时说的是「没有这个信息」。

        存成 0 的后果不是少一条数据，是**反向事实**：下游会读成
        「这个环节一扩产就能上量」，恰好是卡脖子判断的反面。
        """
        assert _safe_int_or_none(0) is None
        assert _safe_int_or_none("0") is None
        assert _safe_int_or_none(18) == 18
        assert _safe_int_or_none("18个月") == 18

    @pytest.mark.parametrize("v", [None, "", "unknown", "N/A", "未知", "不确定", "不详", "-"])
    def test_unknown_writings_all_collapse_to_none(self, v):
        assert _safe_int_or_none(v) is None
        assert _safe_str_or_none(v) is None
        assert _safe_structure(v) is None
        assert _safe_risk(v) is None

    @pytest.mark.parametrize(("raw", "want"), [
        ("single", "single"), ("monopoly", "single"), ("独家", "single"), ("单一", "single"),
        ("oligopoly", "oligopoly"), ("寡头", "oligopoly"), ("duopoly", "oligopoly"),
        ("multi", "multi"), ("多家", "multi"), ("竞争", "multi"),
        # prompt 里写的就是这种带括号的形态，LLM 照抄是常见行为
        ("single(独家)", "single"), ("oligopoly（寡头 2-3 家）", "oligopoly"),
    ])
    def test_structure_aliases(self, raw, want):
        assert _safe_structure(raw) == want

    @pytest.mark.parametrize(("raw", "want"), [
        ("high", "high"), ("高", "high"),
        ("medium", "medium"), ("中", "medium"),
        ("low", "low"), ("低", "low"), ("无", "low"),
    ])
    def test_risk_aliases(self, raw, want):
        assert _safe_risk(raw) == want

    def test_unrecognized_structure_is_none_not_guessed(self):
        """认不出就 None —— 尤其不能做子串猜测。

        `"供不应求"` 里含 `"不应"`，与供应结构毫无关系；把它读成某个
        枚举值就是**编造事实**，比 None 坏得多，因为下游会当真。
        """
        assert _safe_structure("供不应求") is None
        assert _safe_structure("供应结构待查") is None
        assert _safe_risk("风险可控但需观察") is None


# ── 采集侧：模型默认值 ───────────────────────────────────────
class TestModelDefaults:
    def test_constraint_fields_default_to_none_not_placeholder(self):
        """不传就是 None（= 未采集），**不是** 0 个月 / "multi" 这类默认值。

        与 `AlphaScore` 同一原则：None 是「不知道」，5.0 是「中等」，
        两者不能互相冒充。
        """
        n = IndustryNode(name="X", description="d", layer=1,
                         layer_type=LayerType.MATERIAL, function="f")
        assert n.supply_structure is None
        assert n.capacity_lead_time_months is None
        assert n.qualification_cycle_months is None
        assert n.geo_concentration is None
        assert n.export_control_risk is None

    def test_negative_months_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            IndustryNode(name="X", description="d", layer=1,
                         layer_type=LayerType.MATERIAL, function="f",
                         capacity_lead_time_months=-1)


# ── 采集侧：拆解真的把字段落到节点上 ─────────────────────────
class TestDecomposerWritesTheFields:
    @pytest.mark.asyncio
    async def test_fields_from_llm_json_land_on_the_node(self):
        llm = AsyncMock()
        msg = MagicMock()
        msg.content = json.dumps([{
            "name": "磷化铟衬底", "description": "衬底", "function": "外延基板",
            "key_parameters": [], "upstream_deps": [], "dependency": 0.9,
            "alternatives": 1, "notes": "",
            "supply_structure": "寡头", "capacity_lead_time_months": 24,
            "qualification_cycle_months": "18个月", "geo_concentration": "日本 90%",
            "export_control_risk": "高",
        }], ensure_ascii=False)
        llm.ainvoke = AsyncMock(return_value=msg)

        graph = await ChainDecomposer(llm=llm, max_depth=1, sector="半导体").decompose("芯片")
        node = graph.get_node("磷化铟衬底")
        assert node is not None
        assert node.supply_structure == "oligopoly"
        assert node.capacity_lead_time_months == 24
        assert node.qualification_cycle_months == 18
        assert node.geo_concentration == "日本 90%"
        assert node.export_control_risk == "high"

    @pytest.mark.asyncio
    async def test_absent_fields_stay_none(self):
        """LLM 不提这些字段（旧 prompt 下的常态）→ 全部 None，不塞默认值。"""
        llm = AsyncMock()
        msg = MagicMock()
        msg.content = json.dumps([{
            "name": "HBM", "description": "内存", "function": "存储",
            "key_parameters": [], "upstream_deps": [], "dependency": 0.9,
            "alternatives": 1, "notes": "",
        }], ensure_ascii=False)
        llm.ainvoke = AsyncMock(return_value=msg)

        graph = await ChainDecomposer(llm=llm, max_depth=1, sector="GPU").decompose("GPU")
        node = graph.get_node("HBM")
        assert node.supply_structure is None
        assert node.capacity_lead_time_months is None
        assert node.qualification_cycle_months is None
        assert node.geo_concentration is None
        assert node.export_control_risk is None


# ── 消费侧：**本批的哨兵本体** ───────────────────────────────
class _FakeLLM:
    """返回固定 JSON 并记录收到的 prompt。"""

    def __init__(self):
        self.last_prompt = ""

    async def ainvoke(self, messages):
        self.last_prompt = messages[-1].content

        class _Resp:
            content = json.dumps({
                "cr3_estimate": 40, "hhi_estimate": 900,
                "scores": [
                    {"dimension": "scarcity", "score": 4, "reasoning": "t"},
                    {"dimension": "irreplaceability", "score": 5, "reasoning": "t"},
                    {"dimension": "supply_demand_gap", "score": 5, "reasoning": "t"},
                    {"dimension": "pricing_power", "score": 4, "reasoning": "t"},
                    {"dimension": "tech_barrier", "score": 6, "reasoning": "t"},
                ],
                "key_insights": ["x"], "risks": ["y"],
            })
        return _Resp()


def _graph(node: IndustryNode) -> ChainGraph:
    """默认**不带任何约束事实**（alternatives=0、notes 空），
    让「什么都没采到」的用例真的什么都没采到。"""
    root = IndustryNode(name="芯片", description="终端", layer=0,
                        layer_type=LayerType.END_PRODUCT, function="终端")
    return ChainGraph(sector="半导体", end_product="芯片", nodes=[root, node],
                      links=[ChainLink(upstream=node.name, downstream="芯片",
                                       dependency=0.8, alternatives=0)])


def _run(node: IndustryNode) -> str:
    fake = _FakeLLM()
    analyzer = BottleneckAnalyzer(llm=fake, market="us_stock")  # 美股：不碰网络
    asyncio.run(analyzer._analyze_node(node.name, node.description, node.layer, _graph(node)))
    return fake.last_prompt


class TestConstraintFactsReachThePrompt:
    def test_all_five_reach_the_analyze_prompt(self):
        """哨兵本体：采到的约束必须在**实际发给 LLM 的 prompt** 里出现。

        修复前 `_build_context` 只吐「当前/上游/下游环节」，五个字段
        一个都进不去 —— 字段加了、没人用，正是本工作区要根除的那类假闭环。
        """
        node = IndustryNode(
            name="磷化铟衬底", description="衬底", layer=2,
            layer_type=LayerType.MATERIAL, function="外延基板",
            supply_structure="oligopoly", capacity_lead_time_months=24,
            qualification_cycle_months=18, geo_concentration="日本 90%",
            export_control_risk="high",
        )
        prompt = _run(node)
        assert "该环节的已知结构性事实" in prompt
        assert "oligopoly" in prompt
        assert "24" in prompt and "产能扩张周期" in prompt
        assert "18" in prompt and "认证周期" in prompt
        assert "日本 90%" in prompt
        assert "high" in prompt and "出口管制" in prompt

    def test_nothing_collected_emits_no_section_at_all(self):
        """一个字段都没有时**整段不出现** —— 不能留个空标题。

        空标题会让 LLM 以为「已确认无约束」，比不提更坏：
        它会把「没采集到」读成「这个环节没有卡脖子风险」。
        """
        node = IndustryNode(name="通用钢材", description="大宗", layer=2,
                            layer_type=LayerType.MATERIAL, function="结构材料")
        prompt = _run(node)
        assert "已知结构性事实" not in prompt

    def test_partial_collection_emits_only_what_exists(self):
        """只采到一项就只说一项 —— 不补默认值，也不写「其余未知」占位。"""
        node = IndustryNode(name="光刻机", description="光刻", layer=2,
                            layer_type=LayerType.EQUIPMENT, function="光刻",
                            supply_structure="single")
        prompt = _run(node)
        assert "该环节的已知结构性事实" in prompt
        assert "供应结构: single" in prompt
        # 断言**带标签的行**而不是裸词：「认证周期」四个字本来就在
        # DIMENSION_DESC 的 tech_barrier 说明里，裸词断言会假红。
        assert "产能扩张周期:" not in prompt
        assert "客户认证周期:" not in prompt
        assert "地理集中度:" not in prompt
        assert "出口管制" not in prompt


# ── 消费侧：`ChainLink.notes` —— 已在库里躺着的约束事实 ──────
class TestLinkNotesReachThePrompt:
    """生产 14064 条 link 里 13960 条 `notes` 非空，内容就是约束事实本身，
    而全仓**没有一个读取方**。接上它，14 天内的缓存链不用重拆就能受益。"""

    def _graph_with_note(self, notes, upstream="高端纯化填料", alternatives=1):
        root = IndustryNode(name="芯片", description="终端", layer=0,
                            layer_type=LayerType.END_PRODUCT, function="终端")
        node = IndustryNode(name="高端纯化填料", description="填料", layer=2,
                            layer_type=LayerType.MATERIAL, function="分离纯化")
        return ChainGraph(sector="医药", end_product="芯片", nodes=[root, node], links=[
            ChainLink(upstream=upstream, downstream="芯片", dependency=0.8,
                      alternatives=alternatives, notes=notes)])

    def _prompt(self, graph):
        fake = _FakeLLM()
        analyzer = BottleneckAnalyzer(llm=fake, market="us_stock")
        asyncio.run(analyzer._analyze_node("高端纯化填料", "填料", 2, graph))
        return fake.last_prompt

    def test_link_notes_reach_the_prompt(self):
        """真实生产样本原样进 prompt。"""
        note = "高端纯化填料被GE(现Cytiva)、Waters等外资厂商垄断，国内巨化、纳微科技在追赶"
        prompt = self._prompt(self._graph_with_note(note))
        assert "Cytiva" in prompt
        assert "已知替代方案: 1 个" in prompt

    def test_empty_notes_add_nothing(self):
        """notes 为空是常态（生产有 104 条）—— 不留空行、不写占位。"""
        prompt = self._prompt(self._graph_with_note("", alternatives=0))
        assert "拆解备注" not in prompt
        assert "已知替代方案" not in prompt

    def test_alternatives_survive_without_notes(self):
        """`notes` 与 `alternatives` 是两个独立事实，不能因为前者空就丢掉后者。

        生产里就有这种行：有 alternatives 没 notes。把两者绑在一个条件上，
        等于让「没写备注」静默吞掉「已知替代方案 2 个」—— 正是本批要修的
        「一句话弄丢一个事实」。
        """
        prompt = self._prompt(self._graph_with_note("", alternatives=2))
        assert "拆解备注" not in prompt
        assert "已知替代方案: 2 个" in prompt

    def test_other_nodes_notes_do_not_leak(self):
        """别的环节的 link 不能串台 —— 只认 `upstream == 当前环节`。

        串台比缺失更坏：会把邻居的约束当成自己的，直接污染打分。
        """
        prompt = self._prompt(self._graph_with_note("这是别的环节的备注", upstream="其他材料"))
        assert "别的环节" not in prompt

    def test_multi_link_notes_dedup_and_alternatives_merge(self):
        """同一环节多条出边（生产里通用名撞名最多 24 条）：

        - 重复 notes 只出一次；
        - alternatives 合成一行，不能出现「1 个 / 2 个 / 3 个」互相矛盾的多行；
        - notes 最多 5 条，防 prompt 被撞名节点撑爆。
        """
        root = IndustryNode(name="芯片", description="终端", layer=0,
                            layer_type=LayerType.END_PRODUCT, function="终端")
        node = IndustryNode(name="高端纯化填料", description="填料", layer=2,
                            layer_type=LayerType.MATERIAL, function="分离纯化")
        links = [ChainLink(upstream="高端纯化填料", downstream="芯片", dependency=0.8,
                           alternatives=a, notes=n)
                 for a, n in [(1, "重复"), (1, "重复"), (3, "b"), (2, "c"), (2, "d"), (2, "e"), (2, "f")]]
        graph = ChainGraph(sector="医药", end_product="芯片", nodes=[root, node], links=links)
        prompt = self._prompt(graph)
        assert prompt.count("拆解备注: 重复") == 1
        assert prompt.count("拆解备注:") == 5
        assert prompt.count("已知替代方案") == 1
        assert "已知替代方案: 1~3 个" in prompt


# ── Batch 6B：供需缺口锚点 ─────────────────────────────────────
class TestSupplyDemandAnchor:
    """权重最高的 supply_demand_gap 以前既无锚点、也无「无数据」声明 ——
    LLM 只能照着 prompt 示例编。两种状态都必须在 prompt 里明说。"""

    def test_lead_time_becomes_the_anchor(self):
        node = IndustryNode(name="磷化铟衬底", description="衬底", layer=2,
                            layer_type=LayerType.MATERIAL, function="外延基板",
                            capacity_lead_time_months=30)
        prompt = _run(node)
        assert "## 供需缺口锚点" in prompt
        assert "约 30 个月" in prompt
        assert "无数据锚" not in prompt
        assert prompt.count("30 个月") == 1  # 不在结构性事实段重复一次

    def test_no_lead_time_says_so_explicitly(self):
        node = IndustryNode(name="通用钢材", description="大宗", layer=2,
                            layer_type=LayerType.MATERIAL, function="结构材料")
        prompt = _run(node)
        assert "## 供需缺口：无数据锚" in prompt
        assert "保守打分" in prompt
        assert "供需缺口锚点" not in prompt
