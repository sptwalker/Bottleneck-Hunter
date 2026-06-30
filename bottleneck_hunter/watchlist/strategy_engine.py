"""策略大脑引擎 — 情报聚合 + LLM 策略生成

核心循环：
1. refresh_intelligence: 聚合所有 DB 数据 → 情报快照
2. refresh_strategy: 读取情报 + 历史策略 → LLM 推理 → 8 板块策略
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from bottleneck_hunter.llm_clients.factory import get_llm_for_position
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.budget import BudgetTracker

logger = logging.getLogger(__name__)


def _sse(event: str, **data) -> dict:
    """SSE event格式化"""
    return {"event": event, "data": data}



# ─────────────────────────────────────────────────────────
# 情报聚合
# ─────────────────────────────────────────────────────────

async def refresh_intelligence_all(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """刷新所有股票的情报，yield SSE 事件"""
    tickers_list = store.get_tickers()
    total = len(tickers_list)

    yield _sse("intel_start", total=total, message=f"开始聚合 {total} 只股票的情报数据")

    completed = 0
    failed = 0

    for ticker in tickers_list:
        entry = store.get_by_ticker(ticker)
        if not entry:
            continue

        entry_id = entry["id"]

        try:
            async for evt in refresh_intelligence_one(ticker, entry_id, store, budget):
                yield evt
            completed += 1
            yield _sse("intel_progress", completed=completed, failed=failed, total=total,
                       message=f"情报聚合进度 [{completed}/{total}]：{ticker} 完成")
        except Exception as e:
            logger.exception("Intelligence refresh failed for %s", ticker)
            failed += 1
            yield _sse("stock_intel_error", ticker=ticker, error=str(e),
                       completed=completed, failed=failed, total=total,
                       message=f"情报聚合进度 [{completed + failed}/{total}]：{ticker} 失败")

    yield _sse("intel_done", completed=completed, failed=failed, total=total,
               message=f"情报聚合完成：成功 {completed}，失败 {failed}")


async def refresh_intelligence_one(
    ticker: str, entry_id: str, store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """单只股票情报聚合"""
    intel_id, version = store.create_intelligence(entry_id, ticker)

    yield _sse("stock_intel_start", ticker=ticker, version=version,
               message=f"{ticker} 开始聚合情报（v{version}）")

    try:
        entry = store.get(entry_id)

        yield _sse("stock_intel_progress", ticker=ticker, step="aggregating",
                   message=f"{ticker} 正在读取数据源（价格/新闻/SEC/期权/财报）...")

        # 并行读取所有数据源
        results = await asyncio.gather(
            _aggregate_price(ticker, store),
            _aggregate_news(ticker, store),
            _aggregate_sec(ticker, store),
            _aggregate_options(ticker, store),
            _aggregate_earnings(ticker, store),
            _aggregate_source_scorecard(entry_id, entry),
            return_exceptions=True,
        )

        price_summary, news_summary, sec_summary, options_summary, earnings_summary, scorecard_summary = results

        # 处理异常
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("Aggregation failed for %s subsource %d: %s", ticker, i, r)
                results[i] = {}

        aggregated = {
            "price": price_summary if not isinstance(price_summary, Exception) else {},
            "news": news_summary if not isinstance(news_summary, Exception) else {},
            "sec": sec_summary if not isinstance(sec_summary, Exception) else {},
            "options": options_summary if not isinstance(options_summary, Exception) else {},
            "earnings": earnings_summary if not isinstance(earnings_summary, Exception) else {},
            "scorecard": scorecard_summary if not isinstance(scorecard_summary, Exception) else {},
        }

        # LLM 生成简报（可选）
        brief_text = ""
        key_signals = []
        llm, _, _ = get_llm_for_position(position="watchlist_strategy")

        if llm and budget and budget.can_spend(estimated_tokens=1500):
            yield _sse("stock_intel_progress", ticker=ticker, step="llm_brief",
                       message=f"{ticker} 正在调用 LLM 生成情报简报...")
            try:
                brief_text, key_signals = await _generate_brief(llm, ticker, aggregated, budget)
                yield _sse("stock_intel_progress", ticker=ticker, step="brief_generated",
                           message=f"{ticker} 情报简报已生成")
            except Exception as e:
                logger.warning("Brief generation failed for %s: %s", ticker, e)

        # 存储
        store.complete_intelligence(
            intel_id,
            price_summary=json.dumps(aggregated["price"], ensure_ascii=False),
            news_summary=json.dumps(aggregated["news"], ensure_ascii=False),
            sec_summary=json.dumps(aggregated["sec"], ensure_ascii=False),
            options_summary=json.dumps(aggregated["options"], ensure_ascii=False),
            earnings_summary=json.dumps(aggregated["earnings"], ensure_ascii=False),
            source_scorecard_summary=json.dumps(aggregated["scorecard"], ensure_ascii=False),
            brief_text=brief_text,
            key_signals=json.dumps(key_signals, ensure_ascii=False),
            data_freshness=json.dumps({
                "price": aggregated["price"].get("latest_date", ""),
                "news": aggregated["news"].get("latest_date", ""),
                "sec": aggregated["sec"].get("latest_date", ""),
                "options": aggregated["options"].get("latest_date", ""),
                "earnings": aggregated["earnings"].get("latest_date", ""),
            }, ensure_ascii=False),
        )

        yield _sse("stock_intel_done", ticker=ticker, version=version, intel_id=intel_id,
                   message=f"{ticker} 情报聚合完成（v{version}）")

    except Exception as e:
        logger.exception("Intelligence aggregation failed for %s", ticker)
        store.fail_intelligence(intel_id, str(e))
        yield _sse("stock_intel_error", ticker=ticker, error=str(e))


async def _aggregate_price(ticker: str, store: WatchlistStore) -> dict:
    """聚合价格与技术面数据"""
    snap = store.get_latest_snapshot(ticker)
    if not snap:
        return {}

    snapshots = store.get_snapshots(ticker, days=30)

    # 计算趋势
    closes = [s["close"] for s in snapshots if s.get("close")]
    if len(closes) >= 2:
        change_30d = ((closes[0] - closes[-1]) / closes[-1]) * 100 if closes[-1] else 0
    else:
        change_30d = 0

    return {
        "latest_price": snap.get("close"),
        "change_pct": snap.get("change_pct"),
        "change_30d": round(change_30d, 2),
        "rsi_14": snap.get("rsi_14"),
        "macd": snap.get("macd"),
        "macd_signal": snap.get("macd_signal"),
        "volume": snap.get("volume"),
        "latest_date": snap.get("date", ""),
    }


async def _aggregate_news(ticker: str, store: WatchlistStore) -> dict:
    """聚合新闻与舆情数据"""
    news_list = store.get_news(ticker, limit=10)
    if not news_list:
        return {}

    # 计算平均情绪
    sentiments = [n.get("sentiment_score", 0) for n in news_list if n.get("sentiment_score") is not None]
    avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0

    positive = sum(1 for n in news_list if n.get("sentiment") == "positive")
    negative = sum(1 for n in news_list if n.get("sentiment") == "negative")

    return {
        "count": len(news_list),
        "avg_sentiment": round(avg_sentiment, 2),
        "positive_count": positive,
        "negative_count": negative,
        "latest_date": news_list[0].get("date", "") if news_list else "",
        "recent_titles": [n.get("title", "") for n in news_list[:3]],
    }


async def _aggregate_sec(ticker: str, store: WatchlistStore) -> dict:
    """聚合SEC公告与内部交易数据"""
    filings = store.get_filings(ticker, limit=5)
    trades = store.get_insider_trades(ticker, limit=5)

    buy_trades = [t for t in trades if t.get("transaction_type", "").lower() in ("buy", "purchase")]
    sell_trades = [t for t in trades if t.get("transaction_type", "").lower() in ("sell", "sale")]

    return {
        "filing_count": len(filings),
        "recent_filings": [f.get("filing_type", "") for f in filings],
        "insider_buy_count": len(buy_trades),
        "insider_sell_count": len(sell_trades),
        "latest_date": filings[0].get("filed_date", "") if filings else "",
    }


async def _aggregate_options(ticker: str, store: WatchlistStore) -> dict:
    """聚合期权异动数据"""
    options = store.get_options(ticker, limit=3)
    if not options:
        return {}

    latest = options[0]

    return {
        "unusual_volume": latest.get("unusual_volume", 0) == 1,
        "put_call_ratio": latest.get("put_call_ratio"),
        "total_volume": latest.get("total_call_volume", 0) + latest.get("total_put_volume", 0),
        "latest_date": latest.get("date", ""),
    }


async def _aggregate_earnings(ticker: str, store: WatchlistStore) -> dict:
    """聚合财报数据"""
    earnings = store.get_earnings(ticker)
    if not earnings:
        return {}

    latest = earnings[0]

    return {
        "latest_date": latest.get("report_date", ""),
        "eps_surprise_pct": latest.get("eps_surprise_pct"),
        "revenue_actual": latest.get("revenue_actual"),
        "guidance": latest.get("guidance", ""),
    }


async def _aggregate_source_scorecard(entry_id: str, entry: dict) -> dict:
    """聚合源分析评分卡数据（如果有）"""
    if not entry or entry.get("source") != "phase4":
        return {}

    source_analysis_id = entry.get("source_analysis_id")
    if not source_analysis_id:
        return {}

    try:
        from bottleneck_hunter.dataflows.store import AnalysisStore
        analysis_store = AnalysisStore()
        analysis = analysis_store.get(source_analysis_id)
        if not analysis or not analysis.get("result_json"):
            return {}

        result = json.loads(analysis["result_json"])
        scorecards = result.get("supplier_scorecards", [])
        ticker = entry["ticker"]

        # 查找匹配的 scorecard
        for sc in scorecards:
            if sc.get("supplier", {}).get("ticker") == ticker:
                return {
                    "overall_score": sc.get("overall_score"),
                    "quality_score": sc.get("quality_score"),
                    "alpha_score": sc.get("alpha_score"),
                    "final_score": sc.get("final_score"),
                    "bottleneck_node": sc.get("bottleneck_node", ""),
                }

        return {}
    except Exception as e:
        logger.warning("Failed to load source scorecard: %s", e)
        return {}


async def _generate_brief(llm, ticker: str, aggregated: dict, budget: BudgetTracker) -> tuple[str, list[dict]]:
    """LLM 生成情报简报"""
    prompt = f"""你是资深投资分析师。请根据以下数据为股票 {ticker} 生成简明情报简报。

价格与技术面：{json.dumps(aggregated["price"], ensure_ascii=False)}
新闻与舆情：{json.dumps(aggregated["news"], ensure_ascii=False)}
SEC与内部交易：{json.dumps(aggregated["sec"], ensure_ascii=False)}
期权异动：{json.dumps(aggregated["options"], ensure_ascii=False)}
财报数据：{json.dumps(aggregated["earnings"], ensure_ascii=False)}
供应链评分：{json.dumps(aggregated["scorecard"], ensure_ascii=False)}

请输出：
1. 一段话简报（50-80字，概括关键信息）
2. 关键信号列表（JSON数组，每项包含 signal(信号名), direction(多/空/中性), strength(1-5)）

格式：
简报段落
---
[{{"signal":"xxx","direction":"多/空/中性","strength":3}}]"""

    try:
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        # 记录消耗
        if budget:
            budget.record("deepseek", "deepseek-chat", 800, 300, "intelligence_brief")

        # 解析
        parts = response.strip().split("---")
        brief = parts[0].strip() if parts else response.strip()

        signals = []
        if len(parts) > 1:
            try:
                signals = json.loads(parts[1].strip())
            except json.JSONDecodeError:
                pass

        return brief, signals

    except Exception as e:
        logger.warning("Brief generation failed: %s", e)
        return "", []


# ─────────────────────────────────────────────────────────
# 策略生成
# ─────────────────────────────────────────────────────────

async def refresh_strategy_all(
    store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """刷新所有股票的策略，yield SSE 事件"""
    tickers_list = store.get_tickers()
    total = len(tickers_list)

    yield _sse("strategy_start", total=total, message=f"开始生成 {total} 只股票的操作策略")

    completed = 0
    failed = 0

    for ticker in tickers_list:
        entry = store.get_by_ticker(ticker)
        if not entry:
            continue

        entry_id = entry["id"]

        try:
            async for evt in refresh_strategy_one(ticker, entry_id, store, budget):
                yield evt
            completed += 1
            yield _sse("strategy_progress", completed=completed, failed=failed, total=total,
                       message=f"策略生成进度 [{completed}/{total}]：{ticker} 完成")
        except Exception as e:
            logger.exception("Strategy refresh failed for %s", ticker)
            failed += 1
            yield _sse("stock_strategy_error", ticker=ticker, error=str(e),
                       completed=completed, failed=failed, total=total,
                       message=f"策略生成进度 [{completed + failed}/{total}]：{ticker} 失败")

    yield _sse("strategy_done", completed=completed, failed=failed, total=total,
               message=f"策略生成完成：成功 {completed}，失败 {failed}")


async def refresh_strategy_one(
    ticker: str, entry_id: str, store: WatchlistStore,
    budget: BudgetTracker | None = None,
) -> AsyncGenerator[dict, None]:
    """单只股票策略生成"""
    # 检查是否有情报
    latest_intel = store.get_latest_intelligence(entry_id)
    if not latest_intel:
        yield _sse("stock_strategy_skip", ticker=ticker,
                   message=f"{ticker} 跳过：无情报数据，请先刷新信息")
        return

    # 读取历史策略和 UZI 历史
    previous_strategy = store.get_latest_strategy(entry_id)
    uzi_history = store.get_uzi_history(entry_id, limit=3)

    strategy_id, version = store.create_strategy(entry_id, ticker, latest_intel["id"])

    yield _sse("stock_strategy_start", ticker=ticker, version=version,
               message=f"{ticker} 开始生成策略（v{version}）")

    try:
        # 获取 LLM
        llm, _, _ = get_llm_for_position(position="watchlist_strategy")
        if not llm:
            # Mock 数据
            store.complete_strategy(
                strategy_id,
                intelligence_summary="模拟数据（未配置 LLM）",
                core_logic="当前无 LLM 配置，无法生成策略",
                signal="neutral",
                confidence=5,
            )
            yield _sse("stock_strategy_done", ticker=ticker, version=version,
                       signal="neutral", confidence=5, message=f"{ticker} 策略生成完成（Mock）")
            return

        # 检查预算
        if budget and not budget.can_spend(estimated_tokens=3000):
            store.fail_strategy(strategy_id, "预算不足")
            yield _sse("stock_strategy_error", ticker=ticker, error="预算不足")
            return

        # 构建 prompt
        prompt = _build_strategy_prompt(ticker, latest_intel, previous_strategy, uzi_history)

        yield _sse("stock_strategy_progress", ticker=ticker, step="llm_reasoning",
                   message=f"{ticker} LLM 推理中...")

        # LLM 调用
        response = await asyncio.to_thread(lambda: llm.invoke(prompt).content)

        # 记录消耗
        if budget:
            budget.record("deepseek", "deepseek-chat", 2000, 1500, "strategy_generation")

        # 解析响应
        yield _sse("stock_strategy_progress", ticker=ticker, step="parsing",
                   message=f"{ticker} 正在解析策略结果...")
        sections = _parse_strategy_response(response)

        # 对比上次策略
        if previous_strategy:
            comparison = _compute_strategy_diff(sections, previous_strategy)
            sections["strategy_comparison"] = json.dumps(comparison, ensure_ascii=False)

        # 提取信号和信心
        signal = sections.get("signal", "neutral")
        confidence = sections.get("confidence", 5)

        # 存储
        store.complete_strategy(
            strategy_id,
            intelligence_summary=sections.get("intelligence_summary", ""),
            bull_bear_analysis=sections.get("bull_bear_analysis", "{}"),
            core_logic=sections.get("core_logic", ""),
            action_strategy=sections.get("action_strategy", "{}"),
            risk_control=sections.get("risk_control", "{}"),
            targets_timeline=sections.get("targets_timeline", "{}"),
            strategy_comparison=sections.get("strategy_comparison", "{}"),
            confidence_rating=sections.get("confidence_rating", "{}"),
            signal=signal,
            confidence=confidence,
            reasoning_chain=response,
        )

        yield _sse("stock_strategy_done", ticker=ticker, version=version, strategy_id=strategy_id,
                   signal=signal, confidence=confidence,
                   message=f"{ticker} 策略生成完成（v{version}，{signal} {confidence}/10）")

        # 策略生成后自动提取/更新投资论点
        try:
            from bottleneck_hunter.watchlist.thesis_tracker import create_thesis_from_strategy
            completed_strategy = store.get_latest_strategy(entry_id)
            if completed_strategy:
                await create_thesis_from_strategy(store, entry_id, completed_strategy, budget)
        except Exception as e:
            logger.warning("论点提取失败 %s: %s", ticker, e)

    except Exception as e:
        logger.exception("Strategy generation failed for %s", ticker)
        store.fail_strategy(strategy_id, str(e))
        yield _sse("stock_strategy_error", ticker=ticker, error=str(e))


def _build_strategy_prompt(
    ticker: str, intelligence: dict, previous_strategy: dict | None, uzi_history: list[dict]
) -> str:
    """构建策略 prompt"""
    # 解析情报 JSON
    price = json.loads(intelligence.get("price_summary", "{}"))
    news = json.loads(intelligence.get("news_summary", "{}"))
    sec = json.loads(intelligence.get("sec_summary", "{}"))
    options = json.loads(intelligence.get("options_summary", "{}"))
    earnings = json.loads(intelligence.get("earnings_summary", "{}"))
    scorecard = json.loads(intelligence.get("source_scorecard_summary", "{}"))

    # 上次策略参考
    prev_section = ""
    if previous_strategy:
        prev_signal = previous_strategy.get("signal", "neutral")
        prev_conf = previous_strategy.get("confidence", 5)
        prev_core = previous_strategy.get("core_logic", "")
        prev_action = json.loads(previous_strategy.get("action_strategy", "{}"))
        prev_version = previous_strategy.get("version", 0)
        prev_date = previous_strategy.get("created_at", "")

        prev_section = f"""
## 上次策略参考（版本 {prev_version}，{prev_date[:10]}）
- 信号：{prev_signal}，信心：{prev_conf}/10
- 核心逻辑：{prev_core}
- 操作策略：{json.dumps(prev_action, ensure_ascii=False)}
"""

    # UZI 参考
    uzi_section = ""
    if uzi_history:
        uzi_lines = []
        for u in uzi_history:
            uzi_lines.append(f"- {u.get('analysis_type', '')}: {u.get('summary', '')}（{u.get('completed_at', '')[:10]}）")
        uzi_section = f"""
### UZI 历史分析参考
{chr(10).join(uzi_lines)}
注意：UZI 仅作参考工具，策略判断以上述情报数据为核心依据。
"""

    # 对比指令
    comparison_instruction = ""
    if previous_strategy:
        comparison_instruction = f"与版本 {previous_strategy.get('version', 0)} 的策略进行对比，说明：1）信号是否变化及原因；2）关键假设是否改变；3）目标价是否调整。"
    else:
        comparison_instruction = "这是首次策略，无需对比。简要说明建立此策略的初始依据。"

    prompt = f"""你是一位资深投资策略师。请根据以下情报数据，为 {ticker} 生成详细的操作策略。

## 当前情报数据

### 价格与技术面
{json.dumps(price, ensure_ascii=False, indent=2)}

### 新闻与舆情
{json.dumps(news, ensure_ascii=False, indent=2)}

### SEC公告与内部交易
{json.dumps(sec, ensure_ascii=False, indent=2)}

### 期权异动
{json.dumps(options, ensure_ascii=False, indent=2)}

### 财报数据
{json.dumps(earnings, ensure_ascii=False, indent=2)}

### 供应链评估（如有）
{json.dumps(scorecard, ensure_ascii=False, indent=2)}

{prev_section}

{uzi_section}

---

请严格按以下 8 个板块输出，每个板块用 ## 标题分隔：

## 情报摘要
用 3-5 个要点概括所有数据源的核心发现。标注每个要点的数据来源。

## 多空分析
分别列出：
- 多头论据（至少 2 条，用"✅"标记）
- 空头论据（至少 2 条，用"❌"标记）
每条附具体数据支撑。

## 核心逻辑
用 2-3 句话阐述当前的投资逻辑/主线。

## 操作策略
给出明确的信号和具体条件：
- 当前信号：bullish / neutral / bearish（三选一）
- 买入条件：具体价格或事件触发条件
- 加仓条件：在什么情况下加仓
- 减仓条件：在什么情况下减仓
- 卖出条件：具体止盈或退出条件

## 风险控制
- 止损位：具体价格和百分比
- 建议仓位比例：占总仓位的百分比
- 对冲建议：如有

## 目标与时间
列出 1-3 个价格目标，每个附：
- 目标价格
- 预期时间窗口
- 实现概率估计

## 与上次策略对比
{comparison_instruction}

## 信心评级
- 评分：1-10（整数）
- 理由：1-2 句话解释评分依据

---
请确保每个板块都有实质性内容，不要空洞套话。数据引用要具体。"""

    return prompt


def _parse_strategy_response(response: str) -> dict:
    """解析 LLM 策略响应"""
    sections = {
        "intelligence_summary": "",
        "bull_bear_analysis": "{}",
        "core_logic": "",
        "action_strategy": "{}",
        "risk_control": "{}",
        "targets_timeline": "{}",
        "strategy_comparison": "{}",
        "confidence_rating": "{}",
        "signal": "neutral",
        "confidence": 5,
    }

    # 按 ## 分割板块
    blocks = response.split("##")

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        lines = block.split("\n", 1)
        if len(lines) < 2:
            continue

        title = lines[0].strip()
        content = lines[1].strip()

        if "情报摘要" in title:
            sections["intelligence_summary"] = content

        elif "多空分析" in title:
            # 提取多空论据
            bull_points = []
            bear_points = []
            for line in content.split("\n"):
                if "✅" in line:
                    bull_points.append(line.replace("✅", "").strip().lstrip("- "))
                elif "❌" in line:
                    bear_points.append(line.replace("❌", "").strip().lstrip("- "))
            sections["bull_bear_analysis"] = json.dumps(
                {"bull_points": bull_points, "bear_points": bear_points}, ensure_ascii=False
            )

        elif "核心逻辑" in title:
            sections["core_logic"] = content

        elif "操作策略" in title:
            # 提取信号和条件
            signal = "neutral"
            conditions = {}

            for line in content.split("\n"):
                line_lower = line.lower()
                if "当前信号" in line or "signal" in line_lower:
                    if "bullish" in line_lower or "看多" in line:
                        signal = "bullish"
                    elif "bearish" in line_lower or "看空" in line:
                        signal = "bearish"
                elif "买入条件" in line:
                    conditions["buy"] = line.split("：", 1)[-1].strip() if "：" in line else line
                elif "加仓条件" in line:
                    conditions["add"] = line.split("：", 1)[-1].strip() if "：" in line else line
                elif "减仓条件" in line:
                    conditions["reduce"] = line.split("：", 1)[-1].strip() if "：" in line else line
                elif "卖出条件" in line:
                    conditions["sell"] = line.split("：", 1)[-1].strip() if "：" in line else line

            sections["signal"] = signal
            sections["action_strategy"] = json.dumps(
                {"signal": signal, "conditions": conditions}, ensure_ascii=False
            )

        elif "风险控制" in title:
            risk = {}
            for line in content.split("\n"):
                if "止损位" in line:
                    risk["stop_loss"] = line.split("：", 1)[-1].strip() if "：" in line else line
                elif "仓位比例" in line or "仓位" in line:
                    risk["position_pct"] = line.split("：", 1)[-1].strip() if "：" in line else line
                elif "对冲" in line:
                    risk["hedging"] = line.split("：", 1)[-1].strip() if "：" in line else line
            sections["risk_control"] = json.dumps(risk, ensure_ascii=False)

        elif "目标与时间" in title:
            targets = []
            for line in content.split("\n"):
                if line.strip() and not line.startswith("#"):
                    targets.append({"text": line.strip()})
            sections["targets_timeline"] = json.dumps({"targets": targets}, ensure_ascii=False)

        elif "对比" in title:
            sections["strategy_comparison"] = json.dumps({"text": content}, ensure_ascii=False)

        elif "信心评级" in title:
            conf_score = 5
            reason = content

            # 提取评分
            import re
            match = re.search(r"(\d+)", content)
            if match:
                conf_score = max(1, min(10, int(match.group(1))))

            sections["confidence"] = conf_score
            sections["confidence_rating"] = json.dumps(
                {"score": conf_score, "reasoning": reason}, ensure_ascii=False
            )

    return sections


def _compute_strategy_diff(new_sections: dict, previous_strategy: dict | None) -> dict:
    """对比新旧策略，生成变化日志"""
    changes = []
    prev = previous_strategy or {}

    # 信号变化
    new_signal = new_sections.get("signal", "neutral")
    old_signal = prev.get("signal", "neutral")
    if new_signal != old_signal:
        changes.append(f"信号变化：{old_signal} → {new_signal}")

    # 信心变化
    new_conf = new_sections.get("confidence", 5)
    old_conf = prev.get("confidence", 5)
    if abs(new_conf - old_conf) >= 2:
        changes.append(f"信心显著变化：{old_conf}/10 → {new_conf}/10")

    # 核心逻辑变化
    new_logic = new_sections.get("core_logic", "")
    old_logic = prev.get("core_logic", "")
    if new_logic and old_logic and new_logic != old_logic:
        changes.append("核心逻辑已更新")

    if not changes:
        changes.append("策略框架基本保持，细节微调")

    return {
        "changes": changes,
        "reason": f"基于最新情报数据进行策略更新（第 {new_sections.get('version', 1)} 版）",
    }

