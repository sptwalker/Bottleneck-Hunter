"""模型能力综合测试引擎。

6 个测试维度 — 设计目标：拉开区分度，贴合实际使用场景。
- connectivity: 基础连通性（通/不通）
- json_output: 复杂 JSON 结构化输出能力（嵌套/数组/严格格式）
- chinese_analysis: 中文产业链深度分析（评判分析质量而非关键词命中）
- speed: 响应速度（连续梯度而非台阶）
- scoring_variance: 评分区分度（关注排序正确性而非方差大小）
- instruction_follow: 指令遵循能力（字数限制/格式约束/角色扮演）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from statistics import stdev

from langchain_core.messages import HumanMessage

from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

TEST_DIMENSIONS = [
    "connectivity", "json_output", "chinese_analysis",
    "speed", "scoring_variance", "instruction_follow",
]

# ── connectivity ────────────────────────────────────────

async def test_connectivity(provider: str, model: str) -> dict:
    try:
        llm = create_llm(provider, model, temperature=0.1, with_fallback=False)
        t0 = time.time()
        resp = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content="hi")]),
            timeout=30,
        )
        elapsed = (time.time() - t0) * 1000
        text = resp.content.strip() if resp and resp.content else ""
        return {"score": 10.0 if text else 0.0, "latency_ms": round(elapsed), "response": text[:100]}
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── json_output ─────────────────────────────────────────

_JSON_PROMPTS = [
    # 简单 JSON
    {
        "prompt": '请返回纯 JSON（不要 markdown 代码块）：{"name": "一家A股上市公司", "ticker": "股票代码", "sector": "所属行业"}',
        "check": lambda d: all(k in d for k in ("name", "ticker", "sector")),
        "weight": 1,
    },
    # 嵌套结构
    {
        "prompt": """请返回纯 JSON（不要 markdown 代码块），分析一个产业链环节：
{"node": "环节名", "bottleneck_score": 0到10的数字, "suppliers": [{"name": "供应商A", "share": 0.5}, {"name": "供应商B", "share": 0.3}], "risks": ["风险1", "风险2"]}""",
        "check": lambda d: (
            isinstance(d.get("suppliers"), list) and len(d["suppliers"]) >= 1
            and isinstance(d["suppliers"][0], dict) and "name" in d["suppliers"][0]
            and isinstance(d.get("risks"), list)
            and isinstance(d.get("bottleneck_score"), (int, float))
        ),
        "weight": 2,
    },
    # 严格约束
    {
        "prompt": """返回纯 JSON 数组（不要代码块），恰好包含 3 个对象，每个对象有 key: "rank"(整数1/2/3), "material"(字符串), "criticality"(字符串，只能是"high"/"medium"/"low")。示例格式：[{"rank":1,"material":"...","criticality":"high"},...]""",
        "check": lambda d: (
            isinstance(d, list) and len(d) == 3
            and all(isinstance(x, dict) and x.get("rank") in (1, 2, 3)
                    and x.get("criticality") in ("high", "medium", "low")
                    for x in d)
        ),
        "weight": 3,
    },
]


def _parse_json_response(text: str):
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return json.loads(text)


async def test_json_output(provider: str, model: str) -> dict:
    try:
        llm = create_llm(provider, model, temperature=0.1, with_fallback=False)
        total_weight = sum(t["weight"] for t in _JSON_PROMPTS)
        earned = 0.0
        details = []

        for test in _JSON_PROMPTS:
            passed = False
            try:
                resp = await asyncio.wait_for(
                    llm.ainvoke([HumanMessage(content=test["prompt"])]),
                    timeout=30,
                )
                data = _parse_json_response(resp.content)
                passed = test["check"](data)
            except Exception:
                pass
            if passed:
                earned += test["weight"]
            details.append(passed)

        score = round((earned / total_weight) * 10, 1)
        return {"score": score, "details": details, "earned": earned, "total": total_weight}
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── chinese_analysis ────────────────────────────────────

_CN_ANALYSIS_PROMPT = """请用中文深入分析「新能源汽车动力电池」产业链中"正极材料"环节的竞争格局。

要求：
1. 分析供给侧：主要厂商及市占率格局
2. 分析需求侧：下游需求驱动因素
3. 技术路线：不同正极材料路线对比（如磷酸铁锂 vs 三元锂）
4. 瓶颈判断：该环节是否构成产业链瓶颈，为什么
5. 投资启示：从供应链投资角度给出建议

请控制在 200-400 字之间。"""


def _score_chinese_analysis(text: str) -> tuple[float, dict]:
    """多维度评分，返回 (score, breakdown)。"""
    breakdown = {}

    # 1. 长度合理性 (0-1.5): 200-400字最优，太短太长扣分
    clen = len(text)
    if 200 <= clen <= 500:
        breakdown["length"] = 1.5
    elif 100 <= clen < 200:
        breakdown["length"] = 0.8
    elif 500 < clen <= 800:
        breakdown["length"] = 1.0
    elif clen >= 50:
        breakdown["length"] = 0.3
    else:
        breakdown["length"] = 0.0

    # 2. 结构完整性 (0-2.5): 是否覆盖了5个分析维度
    structure_markers = [
        (["供给", "供应", "厂商", "市占", "产能", "企业"], "supply"),
        (["需求", "下游", "驱动", "增长", "装机"], "demand"),
        (["磷酸铁锂", "三元", "技术路线", "LFP", "NCM", "锰酸", "钠电"], "tech"),
        (["瓶颈", "壁垒", "垄断", "集中度", "卡脖子", "门槛"], "bottleneck"),
        (["投资", "布局", "建议", "机会", "标的", "关注"], "invest"),
    ]
    hits = 0
    for keywords, label in structure_markers:
        if any(k in text for k in keywords):
            hits += 1
    breakdown["structure"] = round(hits / len(structure_markers) * 2.5, 2)

    # 3. 专业深度 (0-3.0): 是否提到具体公司、数据、比较
    depth_markers = [
        (["容百", "当升", "长远锂科", "华友", "德方纳米", "湖南裕能",
          "天赐材料", "恩捷", "宁德时代", "比亚迪", "国轩"], "companies"),
        (["市占率", "%", "万吨", "GWh", "亿", "增长率", "CAGR",
          "同比", "环比", "价格", "成本"], "data"),
        (["对比", "优势", "劣势", "vs", "相比", "而言",
          "高于", "低于", "领先", "落后"], "comparison"),
    ]
    depth_hits = 0
    for keywords, label in depth_markers:
        if any(k in text for k in keywords):
            depth_hits += 1
    breakdown["depth"] = round(depth_hits / len(depth_markers) * 3.0, 2)

    # 4. 逻辑性 (0-1.5): 段落/分点结构、因果连接词
    logic_markers = ["因此", "由于", "导致", "所以", "然而", "但是", "此外",
                     "综上", "总结", "一方面", "另一方面", "首先", "其次"]
    logic_hits = sum(1 for k in logic_markers if k in text)
    breakdown["logic"] = min(round(logic_hits / 4 * 1.5, 2), 1.5)

    # 5. 语言质量 (0-1.5): 中文纯度、无乱码
    cn_chars = sum(1 for c in text if "一" <= c <= "鿿")
    cn_ratio = cn_chars / max(len(text), 1)
    breakdown["language"] = round(min(cn_ratio * 2, 1.0) * 1.5, 2)

    total = sum(breakdown.values())
    return round(min(total, 10.0), 1), breakdown


async def test_chinese_analysis(provider: str, model: str) -> dict:
    try:
        llm = create_llm(provider, model, temperature=0.3, with_fallback=False)
        resp = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=_CN_ANALYSIS_PROMPT)]),
            timeout=60,
        )
        text = resp.content.strip()
        score, breakdown = _score_chinese_analysis(text)
        return {"score": score, "breakdown": breakdown, "length": len(text), "preview": text[:200]}
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── speed ───────────────────────────────────────────────

async def test_speed(provider: str, model: str) -> dict:
    """连续梯度评分，不用台阶。测 2 次取平均。"""
    try:
        llm = create_llm(provider, model, temperature=0.1, with_fallback=False)
        latencies = []
        prompts = [
            "请用一句话描述人工智能的发展趋势。",
            "请用一句话描述半导体产业链的核心瓶颈。",
        ]
        for p in prompts:
            t0 = time.time()
            await asyncio.wait_for(
                llm.ainvoke([HumanMessage(content=p)]),
                timeout=60,
            )
            latencies.append((time.time() - t0) * 1000)

        avg_ms = sum(latencies) / len(latencies)

        # 连续评分: 500ms→10, 1000ms→9, 2000ms→7.5, 5000ms→5, 10000ms→2.5, 20000ms→1
        if avg_ms <= 500:
            score = 10.0
        elif avg_ms <= 20000:
            # 对数衰减: 10 - 2.3 * log2(ms/500)
            import math
            score = max(10.0 - 2.3 * math.log2(avg_ms / 500), 1.0)
        else:
            score = 1.0

        return {"score": round(score, 1), "avg_latency_ms": round(avg_ms), "latencies": [round(x) for x in latencies]}
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── scoring_variance ────────────────────────────────────

_SCORING_SCENARIOS = [
    {"name": "EUV光刻机", "desc": "全球仅ASML能生产，技术壁垒极高，供不应求", "expected": 9.5},
    {"name": "硅片切割液", "desc": "通用工业化学品，多家供应商可替代，价格竞争激烈", "expected": 2.0},
    {"name": "车规级IGBT", "desc": "功率半导体核心器件，国产化率低，英飞凌占主导", "expected": 8.0},
    {"name": "PCB基板", "desc": "印刷电路板基础材料，成熟工艺，供应商众多", "expected": 2.5},
    {"name": "质子交换膜", "desc": "氢燃料电池关键材料，Gore/杜邦垄断，国产刚起步", "expected": 8.5},
]

_SCORING_PROMPT_TEMPLATE = """请对以下产业链环节的"瓶颈程度"打分（0-10分，10分=极度瓶颈，0分=完全无瓶颈）。
环节: {name}
描述: {desc}
只返回一个 JSON: {{"score": 数字, "reason": "一句话理由"}}"""


async def test_scoring_variance(provider: str, model: str) -> dict:
    """同时考核：方差（区分度）+ 排序正确性（是否识别出高低瓶颈）。"""
    try:
        llm = create_llm(provider, model, temperature=0.1, with_fallback=False)
        scores = []
        for scenario in _SCORING_SCENARIOS:
            try:
                prompt = _SCORING_PROMPT_TEMPLATE.format(**scenario)
                resp = await asyncio.wait_for(
                    llm.ainvoke([HumanMessage(content=prompt)]),
                    timeout=30,
                )
                data = _parse_json_response(resp.content)
                s = float(data.get("score", 5))
                scores.append(min(max(s, 0), 10))
            except Exception:
                scores.append(5.0)

        expected = [s["expected"] for s in _SCORING_SCENARIOS]

        # 1. 方差分 (0-4): 区分度
        if len(scores) >= 3:
            var = stdev(scores)
        else:
            var = 0.0
        if var >= 3.0:
            var_score = 4.0
        elif var >= 2.0:
            var_score = 3.0
        elif var >= 1.0:
            var_score = 2.0
        elif var >= 0.5:
            var_score = 1.0
        else:
            var_score = 0.0

        # 2. 排序正确性 (0-4): Spearman rank correlation
        def _rank(arr):
            sorted_indices = sorted(range(len(arr)), key=lambda i: arr[i])
            ranks = [0.0] * len(arr)
            for rank, idx in enumerate(sorted_indices):
                ranks[idx] = rank + 1.0
            return ranks

        r_actual = _rank(scores)
        r_expected = _rank(expected)
        n = len(scores)
        d_sq_sum = sum((a - b) ** 2 for a, b in zip(r_actual, r_expected))
        rho = 1 - (6 * d_sq_sum) / (n * (n * n - 1)) if n > 1 else 0
        rank_score = max(0, rho) * 4.0

        # 3. 绝对准确度 (0-2): 与预期分数的平均偏差
        avg_err = sum(abs(a - e) for a, e in zip(scores, expected)) / n
        if avg_err <= 1.0:
            acc_score = 2.0
        elif avg_err <= 2.0:
            acc_score = 1.5
        elif avg_err <= 3.0:
            acc_score = 1.0
        elif avg_err <= 4.0:
            acc_score = 0.5
        else:
            acc_score = 0.0

        total = round(min(var_score + rank_score + acc_score, 10.0), 1)
        return {
            "score": total,
            "variance": round(var, 2),
            "rank_correlation": round(rho, 3),
            "avg_error": round(avg_err, 2),
            "raw_scores": scores,
            "expected": expected,
            "breakdown": {"variance": var_score, "ranking": round(rank_score, 1), "accuracy": acc_score},
        }
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── instruction_follow (新增维度) ──────────────────────

_INSTRUCTION_TESTS = [
    {
        "prompt": "请列出3个中国半导体公司，每行一个，只写公司名，不要编号，不要其他文字。",
        "check": lambda text: (
            2 <= len([l for l in text.strip().split("\n") if l.strip()]) <= 4
            and not any(c in text for c in "1.2.3.（）()·-•")
        ),
        "name": "pure_list",
    },
    {
        "prompt": "请用恰好 20 个中文字（不多不少）描述光伏产业的前景。只输出这 20 个字，不要标点。",
        "check": lambda text: (
            15 <= sum(1 for c in text.strip() if "一" <= c <= "鿿") <= 25
        ),
        "name": "exact_length",
    },
    {
        "prompt": '你是一个只会用 JSON 回答问题的机器人。用户问：A股有多少家上市公司？请回答。\n记住：只返回 JSON，格式为 {"answer": "..."}，绝对不要返回其他任何内容。',
        "check": lambda text: (
            text.strip().startswith("{") and '"answer"' in text
        ),
        "name": "role_constraint",
    },
    {
        "prompt": "请分析宁德时代的竞争优势。要求：分3个要点，每个要点用「▶」开头，每点不超过30字。",
        "check": lambda text: (
            text.count("▶") >= 2
        ),
        "name": "format_symbol",
    },
]


async def test_instruction_follow(provider: str, model: str) -> dict:
    try:
        llm = create_llm(provider, model, temperature=0.1, with_fallback=False)
        passed = 0
        details = {}

        for test in _INSTRUCTION_TESTS:
            ok = False
            try:
                resp = await asyncio.wait_for(
                    llm.ainvoke([HumanMessage(content=test["prompt"])]),
                    timeout=30,
                )
                text = resp.content.strip()
                ok = test["check"](text)
            except Exception:
                pass
            details[test["name"]] = ok
            if ok:
                passed += 1

        score = round((passed / len(_INSTRUCTION_TESTS)) * 10, 1)
        return {"score": score, "passed": passed, "total": len(_INSTRUCTION_TESTS), "details": details}
    except Exception as e:
        return {"score": 0.0, "error": str(e)[:200]}


# ── 注册 ────────────────────────────────────────────────

_TEST_FUNCS = {
    "connectivity": test_connectivity,
    "json_output": test_json_output,
    "chinese_analysis": test_chinese_analysis,
    "speed": test_speed,
    "scoring_variance": test_scoring_variance,
    "instruction_follow": test_instruction_follow,
}


async def run_comprehensive_test(provider: str, model: str) -> dict[str, dict]:
    """运行所有维度测试，返回 {dimension: result_dict}。"""
    results = {}
    for dim in TEST_DIMENSIONS:
        func = _TEST_FUNCS[dim]
        try:
            results[dim] = await func(provider, model)
        except Exception as e:
            results[dim] = {"score": 0.0, "error": str(e)[:200]}
    return results


def compute_composite_score(test_results: dict[str, dict],
                            weights: dict[str, float]) -> float:
    total_w = sum(weights.values())
    if total_w <= 0:
        return 0.0
    score = sum(
        test_results.get(dim, {}).get("score", 0) * w
        for dim, w in weights.items()
    )
    return round(score / total_w, 2)
