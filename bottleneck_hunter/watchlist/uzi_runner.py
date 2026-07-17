"""UZI analysis runner — simplified execution engine for watchlist integration.

Provides async generators that yield SSE events for real-time progress feedback.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from bottleneck_hunter.llm_clients.factory import get_llm_for_position
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

ANALYSIS_TYPES = {"deep-analysis", "investor-panel", "lhb-analyzer", "trap-detector"}


async def run_uzi_analysis(
    ticker: str,
    analysis_type: str,
    store: WatchlistStore,
    entry_id: str,
    force: bool = False,
) -> AsyncGenerator[dict, None]:
    """Run a UZI analysis and yield SSE-compatible progress events.

    Yields dicts with keys: event, progress (0-100), message, status, result, analysis_type.
    force=True 则忽略当日缓存强制重跑。
    """
    if analysis_type not in ANALYSIS_TYPES:
        yield {"event": "error", "message": f"未知分析类型: {analysis_type}"}
        return

    # 复用当日已完成的同类型分析：UZI 单次 8~9 次 LLM 调用，同标的同类型当日重复点击/重开
    # 直接返回缓存结果，省算力。force=True 跳过。
    if not force:
        try:
            recent = store.get_recent_completed_uzi(entry_id, analysis_type, max_age_hours=20)
        except Exception:
            recent = None
        if recent and recent.get("result_json"):
            try:
                cached_result = json.loads(recent["result_json"])
            except (json.JSONDecodeError, TypeError):
                cached_result = None
            if cached_result:
                yield {"event": "started", "analysis_id": recent["id"], "progress": 0,
                       "message": f"♻ 复用当日 {_type_label(analysis_type)} 结果（省重复分析）"}
                yield {"event": "completed", "status": "completed", "progress": 100,
                       "message": "复用当日已完成分析", "analysis_type": analysis_type,
                       "analysis_id": recent["id"], "result": cached_result, "reused": True}
                return

    analysis_id = store.create_uzi_analysis(entry_id, ticker, analysis_type)
    yield {"event": "started", "analysis_id": analysis_id, "progress": 0,
           "message": f"开始 {_type_label(analysis_type)}..."}

    try:
        if analysis_type == "deep-analysis":
            result = await _run_deep_analysis(ticker, _progress_cb(analysis_id))
        elif analysis_type == "investor-panel":
            result = await _run_investor_panel(ticker, _progress_cb(analysis_id))
        elif analysis_type == "lhb-analyzer":
            result = await _run_lhb_analyzer(ticker, _progress_cb(analysis_id))
        elif analysis_type == "trap-detector":
            result = await _run_trap_detector(ticker, _progress_cb(analysis_id))
        else:
            result = {}

        summary = _extract_summary(analysis_type, result)
        if result.get("is_mock"):
            summary = "⚠️ 模拟数据（未配置 LLM） · " + summary
        score = result.get("overall_score")
        signal = result.get("signal_distribution", {}).get("dominant")
        trap_level = result.get("trap_level")

        store.complete_uzi_analysis(
            analysis_id,
            result_json=json.dumps(result, ensure_ascii=False),
            summary=summary,
            score=score,
            signal=signal,
            trap_level=trap_level,
        )

        yield {"event": "completed", "status": "completed", "progress": 100,
               "message": "分析完成", "analysis_type": analysis_type,
               "analysis_id": analysis_id, "result": result}

    except Exception as e:
        logger.exception("UZI analysis failed: %s %s", ticker, analysis_type)
        store.fail_uzi_analysis(analysis_id, str(e))
        yield {"event": "error", "status": "failed", "progress": 100,
               "message": f"分析失败: {e}", "analysis_id": analysis_id}


def _type_label(t: str) -> str:
    return {
        "deep-analysis": "深度分析",
        "investor-panel": "投资者评审",
        "lhb-analyzer": "龙虎榜分析",
        "trap-detector": "杀猪盘检测",
    }.get(t, t)


def _progress_cb(analysis_id: str):
    """Returns a list to accumulate progress events (polled by the generator)."""
    return []


def _extract_summary(analysis_type: str, result: dict) -> str:
    if analysis_type == "deep-analysis":
        s = result.get("overall_score", "?")
        return f"综合评分 {s}/10"
    elif analysis_type == "investor-panel":
        c = result.get("panel_consensus", "?")
        return f"共识度 {c}%"
    elif analysis_type == "trap-detector":
        return result.get("trap_level", "未知")
    elif analysis_type == "lhb-analyzer":
        return result.get("summary", "分析完成")
    return ""


# ---------------------------------------------------------------------------
# Deep Analysis (simplified: 8 core dimensions)
# ---------------------------------------------------------------------------

async def _run_deep_analysis(ticker: str, progress: list) -> dict:
    dimensions = {}
    dim_list = [
        ("financials", "财报质量"),
        ("kline", "技术面"),
        ("valuation", "估值水平"),
        ("industry", "行业景气"),
        ("governance", "公司治理"),
        ("capital_flow", "资金面"),
        ("moat", "护城河"),
        ("events", "催化事件"),
    ]

    llm, _, _ = get_llm_for_position(position="watchlist_uzi")
    if not llm:
        return _mock_deep_analysis(ticker)

    for i, (key, label) in enumerate(dim_list):
        progress.append({"progress": int((i / len(dim_list)) * 80), "message": f"分析 {label}..."})
        try:
            score, comment = await _analyze_dimension(llm, ticker, key, label)
            dimensions[f"{i}_{key}"] = {"score": score, "label": label, "comment": comment}
        except Exception as e:
            logger.warning("Dim %s failed for %s: %s", key, ticker, e)
            dimensions[f"{i}_{key}"] = {"score": 5, "label": label, "comment": f"分析异常: {e}"}
        await asyncio.sleep(0.1)

    scores = [d["score"] for d in dimensions.values() if d.get("score")]
    overall = round(sum(scores) / max(len(scores), 1), 1)

    progress.append({"progress": 90, "message": "生成综合评价..."})

    risks = []
    for d in dimensions.values():
        if d.get("score", 5) < 4:
            risks.append(f"{d['label']}: {d.get('comment', '评分偏低')}")

    return {
        "overall_score": overall,
        "dimensions": dimensions,
        "risks": risks[:5],
        "ticker": ticker,
    }


async def _analyze_dimension(llm, ticker: str, key: str, label: str) -> tuple[int, str]:
    prompt = f"""请对股票 {ticker} 的"{label}"维度进行简要评估。
请给出 1-10 的评分和一句话评语。
格式：评分|评语
例如：7|ROE连续3年高于15%，财报质量良好"""

    try:
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
        parts = response.strip().split("|", 1)
        score = max(1, min(10, int(float(parts[0].strip()))))
        comment = parts[1].strip() if len(parts) > 1 else response.strip()
        return score, comment
    except Exception:
        return 5, "评估中"


# ---------------------------------------------------------------------------
# Investor Panel (simplified: 15 representatives from 9 schools)
# ---------------------------------------------------------------------------

async def _run_investor_panel(ticker: str, progress: list) -> dict:
    llm, _, _ = get_llm_for_position(position="watchlist_uzi")
    if not llm:
        return _mock_investor_panel(ticker)

    schools = [
        ("经典价值", "巴菲特", "ROE、护城河、安全边际"),
        ("成长投资", "欧奈尔", "高增速、PEG、创新"),
        ("宏观对冲", "达里奥", "利率周期、汇率、大宗商品"),
        ("技术趋势", "利弗莫尔", "均线、MACD、成交量、趋势"),
        ("中国价投", "段永平", "好生意、好价格、长期持有"),
        ("A股游资", "赵老哥", "龙头战法、题材、涨停板"),
        ("量化系统", "西蒙斯", "因子暴露、动量、价值"),
        ("科技领袖", "黄仁勋", "技术创新、护城河、市场领导"),
        ("AI卡位", "Serenity", "产业链瓶颈、替代性、稀缺性"),
    ]

    investors = []
    for i, (school, name, criteria) in enumerate(schools):
        progress.append({"progress": int((i / len(schools)) * 80),
                        "message": f"评审: {name} ({school})..."})
        try:
            result = await _evaluate_as_investor(llm, ticker, name, school, criteria)
            investors.append(result)
        except Exception as e:
            logger.warning("Investor %s failed: %s", name, e)
            investors.append({
                "name": name, "group": school,
                "signal": "neutral", "score": 50, "confidence": 30,
                "verdict": "数据不足", "reasoning": str(e),
            })
        await asyncio.sleep(0.1)

    bull = sum(1 for inv in investors if inv["signal"] == "bullish")
    neut = sum(1 for inv in investors if inv["signal"] == "neutral")
    bear = sum(1 for inv in investors if inv["signal"] == "bearish")
    total = len(investors)
    consensus = round(bull / max(total, 1) * 100, 1)

    return {
        "panel_consensus": consensus,
        "signal_distribution": {"bullish": bull, "neutral": neut, "bearish": bear,
                                "dominant": "bullish" if bull > bear else "bearish" if bear > bull else "neutral"},
        "investors": investors,
        "ticker": ticker,
    }


async def _evaluate_as_investor(llm, ticker: str, name: str, school: str,
                                 criteria: str) -> dict:
    prompt = f"""你是投资大佬{name}（{school}流派），核心关注: {criteria}。
请对股票 {ticker} 给出你的投资判断。
格式（严格按此输出）：
signal|score|verdict|reasoning
- signal: bullish 或 neutral 或 bearish
- score: 0-100 的整数评分
- verdict: 一个词（如"买入""观望""回避"）
- reasoning: 1-2句理由"""

    response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
    parts = response.strip().split("|", 3)
    signal = parts[0].strip().lower() if len(parts) > 0 else "neutral"
    if signal not in ("bullish", "neutral", "bearish"):
        signal = "neutral"
    score = 50
    try:
        score = max(0, min(100, int(float(parts[1].strip()))))
    except (IndexError, ValueError):
        pass
    verdict = parts[2].strip() if len(parts) > 2 else "观望"
    reasoning = parts[3].strip() if len(parts) > 3 else response.strip()

    return {
        "name": name, "group": school,
        "signal": signal, "score": score, "confidence": 60,
        "verdict": verdict, "reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# LHB Analyzer (龙虎榜 — A-shares only)
# ---------------------------------------------------------------------------

async def _run_lhb_analyzer(ticker: str, progress: list) -> dict:
    progress.append({"progress": 30, "message": "查询龙虎榜数据..."})

    try:
        import akshare as ak
        df = await asyncio.to_thread(lambda: ak.stock_lhb_detail_em(symbol=ticker[:6]))
        if df is not None and not df.empty:
            progress.append({"progress": 70, "message": "分析游资席位..."})
            records = df.head(20).to_dict("records")
            return {
                "ticker": ticker,
                "records": records,
                "count": len(df),
                "summary": f"近期共 {len(df)} 条龙虎榜记录",
            }
    except Exception as e:
        logger.warning("LHB fetch failed for %s: %s", ticker, e)

    return {
        "ticker": ticker,
        "records": [],
        "count": 0,
        "summary": "未找到龙虎榜数据",
    }


# ---------------------------------------------------------------------------
# Trap Detector (8-signal scan)
# ---------------------------------------------------------------------------

async def _run_trap_detector(ticker: str, progress: list) -> dict:
    llm, _, _ = get_llm_for_position(position="watchlist_uzi")
    if not llm:
        return _mock_trap_result(ticker)

    signals = [
        ("低质量账号同时推荐", f"搜索 {ticker} 是否有大量低质量账号同时推荐该股票"),
        ("推荐话术模板化", f"搜索 {ticker} 相关推荐是否使用'即将爆发''主力建仓完毕''目标翻倍'等模板话术"),
        ("付费社群引流", f"搜索 {ticker} 是否有微信群、VIP直播间等付费社群引流行为"),
        ("基本面与热度脱节", f"分析 {ticker} 基本面是否支撑当前的网络热度"),
        ("K线异常配合", f"分析 {ticker} 推荐密集期前是否已大幅拉升"),
        ("股神人设推广", f"搜索 {ticker} 是否有'老师''股神'类人设推广"),
        ("跨平台联动", f"搜索 {ticker} 是否在小红书/抖音/B站/知乎多平台联动推广"),
        ("虚假研报", f"搜索 {ticker} 是否有谣言、辟谣、虚假消息"),
    ]

    hits = []
    for i, (name, check) in enumerate(signals):
        progress.append({"progress": int((i / len(signals)) * 80),
                        "message": f"扫描: {name}..."})
        try:
            prompt = f"""请判断以下情况是否存在（回答"是"或"否"并简要说明）：
{check}
格式：是或否|简要说明"""
            response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)
            parts = response.strip().split("|", 1)
            is_hit = parts[0].strip().startswith("是")
            evidence = parts[1].strip() if len(parts) > 1 else response.strip()
            if is_hit:
                hits.append({"id": i + 1, "name": name, "evidence": evidence, "severity": "medium"})
        except Exception as e:
            logger.warning("Trap signal %d failed: %s", i, e)
        await asyncio.sleep(0.1)

    hit_count = len(hits)
    if hit_count <= 1:
        level, score = "🟢 安全", 9
    elif hit_count <= 3:
        level, score = "🟡 注意", 7
    elif hit_count <= 5:
        level, score = "🟠 警惕", 4
    else:
        level, score = "🔴 高度可疑", 2

    recommendation = {
        "🟢 安全": "未发现明显推广痕迹，可正常分析",
        "🟡 注意": "有少量推广信号，建议核实后再决策",
        "🟠 警惕": "多个推广信号，强烈建议谨慎",
        "🔴 高度可疑": "强烈建议回避，疑似杀猪盘套路",
    }.get(level, "")

    return {
        "ticker": ticker,
        "trap_score": score,
        "trap_level": level,
        "signals_hit": hits,
        "total_signals": 8,
        "recommendation": recommendation,
    }


# ---------------------------------------------------------------------------
# Mock results (when no LLM is configured)
# ---------------------------------------------------------------------------

def _mock_deep_analysis(ticker: str) -> dict:
    return {
        "is_mock": True,
        "overall_score": 6.5,
        "dimensions": {
            f"{i}_{k}": {"score": 5 + (i % 4), "label": l, "comment": "模拟数据（未配置 LLM）"}
            for i, (k, l) in enumerate([
                ("financials", "财报质量"), ("kline", "技术面"),
                ("valuation", "估值水平"), ("industry", "行业景气"),
                ("governance", "公司治理"), ("capital_flow", "资金面"),
                ("moat", "护城河"), ("events", "催化事件"),
            ])
        },
        "risks": ["未配置 LLM，当前为模拟数据"],
        "ticker": ticker,
    }


def _mock_investor_panel(ticker: str) -> dict:
    return {
        "is_mock": True,
        "panel_consensus": 55.0,
        "signal_distribution": {"bullish": 5, "neutral": 3, "bearish": 1, "dominant": "bullish"},
        "investors": [
            {"name": "巴菲特", "group": "经典价值", "signal": "bullish", "score": 70,
             "confidence": 60, "verdict": "买入", "reasoning": "模拟数据"},
        ],
        "ticker": ticker,
    }


def _mock_trap_result(ticker: str) -> dict:
    return {
        "is_mock": True,
        "ticker": ticker,
        "trap_score": 9,
        "trap_level": "🟢 安全",
        "signals_hit": [],
        "total_signals": 8,
        "recommendation": "模拟数据（未配置 LLM）",
    }


