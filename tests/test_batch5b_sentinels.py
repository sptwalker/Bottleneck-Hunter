"""Batch 5B（集中度校准被 z-score 抹平 + 真实集中度取不到）回归哨兵。

生产实证（2026-09-28，8437 个环节 / 31 次分析）：

- `cr3_source` **100% 是 `llm_estimate`**（a_stock 1479 个、us_stock 6381 个），
  `concentration_detail` 非空的节点 **0 个**。该功能 2026-07-03 上线，而所有 A 股
  分析（07-06 … 09-10）都在其**之后** —— 排除「取样早于功能」的混淆，即
  **三个月里真实集中度一次都没用上**。
- reasoning 里出现校准文本的 30 条中，**28 条文本与分数背离**：文本写
  「HHI校准 7→5」，实际 score 是 1.8。因为校准在 `_analyze_node`（标准化**之前**）
  执行，随后 `normalize_scores` 用 `5+2z` 把这两个维度整维重写 —— 校准白做，
  只留下那句自相矛盾的话。

本文件盯住两条不变量：
1. 报告里那句校准文本，**必须与它旁边那个分数一致**（走完整 `analyze()` 流程）；
2. 数据源不可用要能**熔断**，而不是逐节点空等；单个接口挂掉不算数据源故障。
"""
from __future__ import annotations

import asyncio
import json
import re
from types import ModuleType
from unittest.mock import patch

import pytest

from bottleneck_hunter.chain import industry_concentration as ic
from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer, normalize_scores
from bottleneck_hunter.chain.models import ChainGraph, IndustryNode, LayerType

# 校准戳里那个箭头右侧的数字，形如 "[HHI校准: HHI=1200<1500, 8→6] "
_CALIB_RE = re.compile(r"\[(?:HHI|CR3)校准[^\]]*?(\d+(?:\.\d+)?)\s*→\s*(\d+(?:\.\d+)?)\]")


# ── 1. 校准文本必须与分数一致（本条即原缺陷的判定核心） ──────────────

# 每个环节的 scarcity 原始分与真实集中度。
# CR3/HHI 必须**物理自洽**：两家都来自同一批成分股市值，高 CR3 必然高 HHI。
# 写一对矛盾的数（CR3=25 + HHI=2800）会让 HHI 规则往上推、CR3 规则往下拉，
# 两条规则互相抵消 —— 那测的是规则的打架（P2-5，另案），不是本条的收口。
_SPECS = {
    # 真实低集中（HHI=900<1500 且 CR3=25<30）而 LLM 给了 scarcity=9 → 两条规则同向压下去
    "光刻机": {"scarcity": 9.0, "conc": {"cr3": 25.0, "cr5": 45.0, "hhi": 900,
                                       "company_count": 40, "board_name": "光刻机",
                                       "top_companies": [("A", 10.0)], "source": "akshare"}},
    # 真实高集中（HHI=2800>2500 且 CR3=90>80）而 LLM 只给 scarcity=2 → 同向抬上来
    "通用材料": {"scarcity": 2.0, "conc": {"cr3": 90.0, "cr5": 96.0, "hhi": 2800,
                                        "company_count": 7, "board_name": "通用材料",
                                        "top_companies": [("B", 40.0)], "source": "akshare"}},
    "封装测试": {"scarcity": 4.0, "conc": None},
    "PCB": {"scarcity": 3.0, "conc": None},
}


class _NodeAwareLLM:
    """按 prompt 里的环节名返回对应评分，记录每个环节收到的 prompt。"""

    def __init__(self):
        self.prompts: dict[str, str] = {}

    async def ainvoke(self, messages):
        prompt = messages[-1].content
        name = next(n for n in _SPECS if f"分析环节: {n}" in prompt)
        self.prompts[name] = prompt
        spec = _SPECS[name]
        payload = {
            "cr3_estimate": 50, "hhi_estimate": 1800,   # LLM 自估，有真实数据时应被覆盖
            "scores": [
                {"dimension": "scarcity", "score": spec["scarcity"], "reasoning": "r"},
                {"dimension": "irreplaceability", "score": 4, "reasoning": "r"},
                {"dimension": "supply_demand_gap", "score": 5, "reasoning": "r"},
                {"dimension": "pricing_power", "score": 4, "reasoning": "r"},
                {"dimension": "tech_barrier", "score": 6, "reasoning": "r"},
            ],
            "key_insights": ["i"], "risks": ["k"],
        }
        if spec["conc"] is None:      # 该环节没有集中度数据 → 两个字段都缺
            payload["cr3_estimate"] = None
            payload["hhi_estimate"] = None

        class _Resp:
            content = json.dumps(payload)
        return _Resp()


def _graph4() -> ChainGraph:
    root = IndustryNode(name="芯片", description="终端", layer=0,
                        layer_type=LayerType.END_PRODUCT, function="终端产品")
    nodes = [root] + [
        IndustryNode(name=n, description=f"{n}描述", layer=2,
                     layer_type=LayerType.MATERIAL, function="环节")
        for n in _SPECS
    ]
    return ChainGraph(sector="半导体", end_product="芯片", nodes=nodes)


def _analyze_all():
    """跑完整 analyze() 流程，返回 (reports, analyzer 的 LLM)。"""
    with patch.object(ic, "compute_concentration",
                      side_effect=lambda name: _SPECS[name]["conc"]), \
         patch.object(ic, "clear_cache"):
        llm = _NodeAwareLLM()
        analyzer = BottleneckAnalyzer(llm=llm, market="a_stock")
        reports = asyncio.run(analyzer.analyze(_graph4()))
    return reports, llm


def test_calibration_text_never_contradicts_the_score():
    """哨兵本体：凡带校准戳的维度，戳上那个「改成几分」必须等于实际分数。

    修复前必然失败 —— 校准跑在 z-score 之前，分数被整维重写成 `5+2z`，
    戳还留着旧值（生产 30 条里 28 条如此）。
    """
    reports, _ = _analyze_all()

    checked = 0
    for rpt in reports:
        for s in rpt.scores:
            m = _CALIB_RE.search(s.reasoning or "")
            if not m:
                continue
            checked += 1
            claimed = float(m.group(2))
            assert s.score == claimed, (
                f"{rpt.node_name}.{s.dimension} 校准文本说改成 {m.group(2)}，"
                f"实际分数 {s.score} —— 文本与数值背离"
            )
    # 非空洞：至少两个环节（一高一低）真的被校准过，否则上面什么也没验
    assert checked >= 2, f"没有任何校准戳被断言，本测试是空洞的（checked={checked}）"


def test_real_concentration_actually_reaches_the_score():
    """真实集中度必须是**最后**说了算的那个：校准后的 scarcity 反映真实 HHI/CR3。

    「通用材料」真实 HHI=2800/CR3=90%（高集中）而 LLM 只给 2 分 → 必须被抬上来；
    「光刻机」真实 HHI=900/CR3=25%（低集中）但 LLM 给了 9 分 → 必须被压下去。
    若 z-score 在最后覆盖，这两个断言都会失败。
    """
    reports, _ = _analyze_all()
    by_name = {r.node_name: r for r in reports}

    def scarcity(name):
        return next(s.score for s in by_name[name].scores if s.dimension == "scarcity")

    assert by_name["通用材料"].cr3_source == "akshare"      # 真实数据确实用上了
    assert scarcity("通用材料") >= 6.0, "真实高集中度没能把 scarcity 抬起来"
    assert scarcity("光刻机") <= 6.0, "真实低集中度没能把 scarcity 压下去"
    # 有真实数据的两家，CR3/HHI 必须是真实值而非 LLM 自估的 50/1800
    assert (by_name["光刻机"].cr3_estimate, by_name["光刻机"].hhi_estimate) == (25, 900)
    assert (by_name["通用材料"].cr3_estimate, by_name["通用材料"].hhi_estimate) == (90, 2800)


def test_node_without_concentration_data_is_not_calibrated():
    """没有集中度数据的环节不该被重放碰到（生产里 8408/8437 属此类）。"""
    reports, _ = _analyze_all()
    for name in ("封装测试", "PCB"):
        rpt = next(r for r in reports if r.node_name == name)
        assert rpt.cr3_estimate is None and rpt.hhi_estimate is None
        assert not any(_CALIB_RE.search(s.reasoning or "") for s in rpt.scores)
        assert rpt.hhi_adjustments == []


def test_replay_is_idempotent_no_double_push():
    """重放不能把同一次集中度观察**扣两次分**。

    `_check_hhi_consistency` 每个分支都自证伪（只在 <6 时抬到 ≥6，只在 >6 时压到
    ≤6），所以第二次跑必然一个分支都不命中。这条守住「不需要幂等标记」这个前提。
    """
    reports, _ = _analyze_all()
    analyzer = BottleneckAnalyzer(llm=_NodeAwareLLM(), market="a_stock")
    before = {(r.node_name, s.dimension): s.score for r in reports for s in r.scores}
    stamps = {r.node_name: list(r.hhi_adjustments) for r in reports}

    again = analyzer._calibrate_concentration(reports)

    assert again == 0, "第二次重放又改了分数 —— 同一次集中度被扣了两次分"
    after = {(r.node_name, s.dimension): s.score for r in reports for s in r.scores}
    assert before == after
    # 校准戳也不该翻倍（或再挂一句上去）
    assert {r.node_name: list(r.hhi_adjustments) for r in reports} == stamps


def test_counter_reports_only_real_changes():
    """返回值只在**分数真的动了**时才计数，且戳只记真正动了的那个维度。

    HHI 与 CR3 会先后命中同一维度 scarcity（P2-5，另案）。两者方向相反时
    （LLM 自估 CR3=25 + HHI=2800，物理上不可能的一对）：HHI 先把 scarcity 抬到 6、
    CR3 再压回 4，净变化为零 —— 这个维度**不得**留下校准戳，否则 `hhi_adjustments`
    写满「scarcity 4→6」这类根本没生效的条目（生产 30 条校准文本里正有这种误导）。
    同一次里 pricing_power 只被 HHI 规则碰过、确实 4→6，必须计入。
    """
    from bottleneck_hunter.chain.models import BottleneckReport, BottleneckScore

    def _report(cr3, hhi, s=4.0, p=4.0):
        return BottleneckReport(
            node_name="矛盾环节", node_description="d", layer=2,
            scores=[BottleneckScore(dimension=d, score=v, reasoning="r") for d, v in
                    (("scarcity", s), ("pricing_power", p))],
            overall_score=4.0, cr3_estimate=cr3, hhi_estimate=hhi, cr3_source="akshare",
        )

    analyzer = BottleneckAnalyzer(llm=_NodeAwareLLM(), market="a_stock")

    # 自相矛盾的一对：HHI 抬 scarcity、CR3 又压回去 → 净零，不计这一维
    contradiction = _report(cr3=25, hhi=2800)
    assert analyzer._calibrate_concentration([contradiction]) == 1
    assert len(contradiction.hhi_adjustments) == 1
    assert "pricing_power" in contradiction.hhi_adjustments[0]
    scarcity = next(s for s in contradiction.scores if s.dimension == "scarcity")
    assert scarcity.score == 4.0 and "校准" not in scarcity.reasoning, (
        "净变化为零的维度仍被挂了校准戳 —— 文本会宣称一个并未发生的改动"
    )
    # 戳上写的「改成几分」必须就是最终分（HHI 抬到 6 后 CR3 没再动 pricing）
    pricing = next(s for s in contradiction.scores if s.dimension == "pricing_power")
    assert pricing.score == 6.0 and "4→6" in pricing.reasoning

    # 自洽且确实需要校准的一对 → 计入
    genuine = _report(cr3=90, hhi=2800, s=2.0)
    assert analyzer._calibrate_concentration([genuine]) == 1
    assert genuine.hhi_adjustments, "真实高集中度未产生任何校准记录"


def test_normalize_scores_rewrites_anchored_dims():
    """记录前提：z-score 确实会重写 scarcity（这正是校准必须重放的原因）。

    若哪天 `normalize_scores` 不再动这两个维度，重放就该重新评估 —— 本条会提醒。
    """
    reports, _ = _analyze_all()
    raw = {n: s["scarcity"] for n, s in _SPECS.items()}

    class _R:
        pass

    # 用全新一批「未校准」报告复现：raw 分数与 normalize 后不同
    from bottleneck_hunter.chain.models import BottleneckReport, BottleneckScore

    batch = []
    for i, (name, spec) in enumerate(_SPECS.items()):
        batch.append(BottleneckReport(
            node_name=name, node_description="d", layer=2,
            scores=[BottleneckScore(dimension=d, score=spec["scarcity"] if d == "scarcity" else float(i + 1),
                                    reasoning="r")
                    for d in ("scarcity", "irreplaceability", "supply_demand_gap",
                              "pricing_power", "tech_barrier")],
            overall_score=5.0,
        ))
    normalize_scores(batch)
    got = {r.node_name: next(s.score for s in r.scores if s.dimension == "scarcity") for r in batch}
    assert got != raw, "normalize_scores 没有再重写 scarcity —— 重放的前提变了"
    # 且确实被改成了 5+2z 的形态（不再是 LLM 原值）
    for name in raw:
        assert got[name] != raw[name]


def test_prompt_says_so_when_real_concentration_is_missing():
    """取不到真实数据时必须在 prompt 里**明说**。

    此前这里只留空，LLM 看到提示词里的示例 HHI=1800 会以为自己拿到了真实值，
    照着示例编集中度 —— 而下游校准会把这些编出来的数当锚点改分。
    """
    _, llm = _analyze_all()
    assert "无真实数据" in llm.prompts["封装测试"]
    assert "保守估算" in llm.prompts["封装测试"]
    # 有真实数据的那家仍是「直接采用」口径
    assert "真实市场集中度数据" in llm.prompts["光刻机"]
    assert "无真实数据" not in llm.prompts["光刻机"]


# ── 2. 数据源：概念优先、单接口挂掉不算故障、缓存不写死 ──────────────
#
# 用真 pandas DataFrame 而不是手搓的假对象：pandas 内部会拿**布尔 Series**
# 去 `__getitem__`，任何手写的 `df[df["板块名称"].str.contains(...)]` 替身都会
# 在真实调用路径上崩掉 —— 那样测的就不是生产路径了。

def _board_df(names: list[str]):
    import pandas as pd
    return pd.DataFrame({"板块名称": names})


def _cons_df(mcaps: list[float]):
    """成分股表：市值用『元』（akshare 实际口径），字段名带「市值」。"""
    import pandas as pd
    return pd.DataFrame({"名称": [f"C{i}" for i in range(len(mcaps))], "总市值": mcaps})


def _fake_akshare(*, concept_ok=True, industry_ok=True, mcaps=None):
    """构造一个最小 akshare 替身模块。

    mcaps 默认 50/30/20 亿 → CR3=100, HHI=3800（三家全在头部）。
    """
    ak = ModuleType("akshare")
    cons = mcaps if mcaps is not None else [50e8, 30e8, 20e8]

    def _concept_list():
        if not concept_ok:
            raise ConnectionError("RemoteDisconnected")
        return _board_df(["光刻机", "PCB", "先进封装"])

    def _industry_list():
        if not industry_ok:
            raise ConnectionError("RemoteDisconnected")
        return _board_df(["半导体", "白酒"])

    ak.stock_board_concept_name_em = _concept_list
    ak.stock_board_industry_name_em = _industry_list
    ak.stock_board_concept_cons_em = lambda symbol=None: _cons_df(cons)
    ak.stock_board_industry_cons_em = lambda symbol=None: _cons_df(cons)
    return ak


@pytest.fixture(autouse=True)
def _clean_caches():
    """模块级缓存与类级熔断计数跨测试共享，必须清干净。"""
    ic.clear_cache()
    BottleneckAnalyzer._real_conc_consecutive_fails = 0
    yield
    ic.clear_cache()
    BottleneckAnalyzer._real_conc_consecutive_fails = 0


def test_concept_board_is_tried_first():
    """节点名逐字命中概念板块（光刻机/PCB/先进封装），行业板块只是粗分类。

    此前顺序是 industry 在前，而那个接口实测连续 5-6 次 RemoteDisconnected ——
    最稳、也真正对得上名字的来源被最不稳的来源挡在后面，白白降级。
    """
    with patch.dict("sys.modules", {"akshare": _fake_akshare()}):
        got = ic.compute_concentration("光刻机")
    assert got is not None
    assert got["board_name"] == "光刻机"
    assert got["source"] == "akshare"
    assert got["cr3"] == 100.0 and got["hhi"] == 3800


def test_one_dead_endpoint_still_yields_data():
    """行业板块接口挂掉不算数据源故障 —— 概念板块能给数据就必须给。"""
    with patch.dict("sys.modules", {"akshare": _fake_akshare(industry_ok=False)}):
        got = ic.compute_concentration("光刻机")     # 不得抛 ProbeFailure
    assert got is not None and got["cr3"] == 100.0


def test_both_endpoints_dead_raises_probe_failure():
    """两个板块列表都拉不到 → 抛 ProbeFailure，让调用方熔断（别逐节点空等）。"""
    with patch("bottleneck_hunter.chain.industry_concentration._BOARD_LIST_RETRIES", 1), \
         patch.dict("sys.modules", {"akshare": _fake_akshare(concept_ok=False, industry_ok=False)}), \
         pytest.raises(ic.ProbeFailure):
        ic.compute_concentration("光刻机")


def test_no_match_is_none_not_probe_failure():
    """接口正常但没有匹配板块 = 正常的降级，返回 None，绝不抛。"""
    with patch.dict("sys.modules", {"akshare": _fake_akshare()}):
        assert ic.compute_concentration("某冷门环节") is None


def test_failure_is_not_cached_as_none():
    """失败的板块**不得**写进缓存。

    此前把 None 也缓存，且 `clear_cache()` 在生产代码里零调用 —— 一次抖动就把
    该板块在进程存续期内永久判死。
    """
    with patch("bottleneck_hunter.chain.industry_concentration._BOARD_LIST_RETRIES", 1), \
         patch.dict("sys.modules", {"akshare": _fake_akshare(concept_ok=False, industry_ok=False)}), \
         pytest.raises(ic.ProbeFailure):
        ic.compute_concentration("光刻机")

    # 数据源恢复后必须立刻能拿到数据（缓存里不该有「光刻机=None」）
    # 注：板块列表成功过一次后会被缓存，所以这里连 `_BOARD_LIST_CACHE` 一起清，
    # 模拟「下一次分析」；关键是 `_CONCENTRATION_CACHE` 里不得留下失败的判死记录。
    ic._BOARD_LIST_CACHE.clear()
    with patch.dict("sys.modules", {"akshare": _fake_akshare()}):
        got = ic.compute_concentration("光刻机")
    assert got is not None and got["cr3"] == 100.0


def test_board_list_fetched_once_per_process():
    """板块列表在一次分析里被每个节点用到，只该拉一次。"""
    calls = {"concept": 0, "industry": 0}
    ak = _fake_akshare()

    def _concept():
        calls["concept"] += 1
        return _board_df(["光刻机", "PCB"])

    def _industry():
        calls["industry"] += 1
        return _board_df(["半导体"])

    ak.stock_board_concept_name_em = _concept
    ak.stock_board_industry_name_em = _industry
    with patch.dict("sys.modules", {"akshare": ak}):
        ic.compute_concentration("光刻机")
        ic.compute_concentration("PCB")
    assert calls == {"concept": 1, "industry": 1}, calls


# ── 3. 熔断器：连续失败要停手，成功要清零 ──────────────────────────

def _analyzer():
    return BottleneckAnalyzer(llm=_NodeAwareLLM(), market="a_stock")


def test_circuit_breaker_trips_after_limit():
    """连续失败到阈值后不再发起调用（每个节点白等 5-6s，上百个节点就是几分钟）。"""
    a = _analyzer()
    with patch.object(ic, "compute_concentration", side_effect=ic.ProbeFailure("dead")) as m:
        for _ in range(a._CONC_FAIL_LIMIT):
            assert asyncio.run(a._fetch_real_concentration("光刻机")) is None
        assert m.call_count == a._CONC_FAIL_LIMIT
        # 已熔断 → 不再调用数据源
        assert asyncio.run(a._fetch_real_concentration("PCB")) is None
        assert m.call_count == a._CONC_FAIL_LIMIT, "熔断后仍在逐节点发起调用"


def test_success_resets_the_failure_counter():
    """成功一次即清零，给数据源恢复留路。

    此前清零写在 `else:` 分支里，而 `try` 体已经 return —— 那段代码永远不可达，
    计数器只增不减，一次抖动污染整个进程。
    """
    a = _analyzer()
    with patch.object(ic, "compute_concentration", side_effect=ic.ProbeFailure("dead")):
        for _ in range(a._CONC_FAIL_LIMIT - 1):
            asyncio.run(a._fetch_real_concentration("x"))
    assert BottleneckAnalyzer._real_conc_consecutive_fails == a._CONC_FAIL_LIMIT - 1

    ok = {"cr3": 80.0, "cr5": 90.0, "hhi": 2600, "company_count": 5,
          "board_name": "光刻机", "top_companies": [], "source": "akshare"}
    with patch.object(ic, "compute_concentration", return_value=ok):
        assert asyncio.run(a._fetch_real_concentration("光刻机")) == ok
    assert BottleneckAnalyzer._real_conc_consecutive_fails == 0, "成功未能清零失败计数"


def test_non_probe_failure_does_not_count_toward_breaker():
    """偶发异常不该累加熔断计数（那是「接口间歇抖动」，不是「数据源整体不可用」）。"""
    a = _analyzer()
    with patch.object(ic, "compute_concentration", side_effect=ValueError("weird")):
        assert asyncio.run(a._fetch_real_concentration("光刻机")) is None
    assert BottleneckAnalyzer._real_conc_consecutive_fails == 0


def test_us_stock_never_probes_the_source():
    """美股无等价免费数据源 → 一个节点都不该发起调用，总线级熔断计数也不该被碰。

    闸门在 `_analyze_node`（`if self.market == "a_stock"`），**不在**
    `_fetch_real_concentration` —— 直接调后者测的是「函数自身不做市场判断」，
    与美股无关。必须走 `_analyze_node` 才是在测生产路径。
    """
    a = BottleneckAnalyzer(llm=_NodeAwareLLM(), market="us_stock")
    with patch.object(ic, "compute_concentration", return_value=None) as m:
        rpt = asyncio.run(a._analyze_node("光刻机", "d", 2, _graph4()))
    m.assert_not_called()
    assert rpt.cr3_source == "llm_estimate"
    assert rpt.cr3_estimate == 50      # LLM 原值，未被真实数据覆盖
    # 熔断计数是类级共享的：美股根本不该往里累加，否则会误伤同进程的 A 股分析
    assert BottleneckAnalyzer._real_conc_consecutive_fails == 0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
