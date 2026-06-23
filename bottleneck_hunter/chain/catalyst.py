"""催化剂时间线分析：识别推动公司价值兑现的关键事件和时间节点。

使用 LLM 识别 5 类催化剂（政策/产能/技术/订单/财报），
结合研报标题关键词和下次财报日期作为辅助数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from .json_utils import strip_fences as _strip_fences
from .models import (
    BottleneckReport,
    CatalystEvent,
    CatalystTimeline,
    FinancialSnapshot,
    SupplierInfo,
    SupplierScorecard,
)

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Prompt file not found: {path}")


def _extract_report_keywords(snap: FinancialSnapshot | None) -> str:
    """从研报相关字段提取辅助信息。"""
    if not snap:
        return ""
    parts = []
    if snap.analyst_report_count is not None:
        parts.append(f"近期研报覆盖: {snap.analyst_report_count}篇")
    if snap.analyst_rating:
        parts.append(f"机构评级: {snap.analyst_rating}")
    if snap.consensus_eps is not None:
        parts.append(f"一致预期EPS: {snap.consensus_eps}")
    return "，".join(parts)


class CatalystAnalyzer:
    """催化剂时间线分析器。"""

    LLM_TIMEOUT = 120

    def __init__(self, llm: BaseChatModel, language: str = "zh"):
        self.llm = llm
        self.language = language
        self._system_prompt = _load_prompt("catalyst")
        self._on_progress = None

    async def analyze(
        self,
        supplier: SupplierInfo,
        bottleneck: BottleneckReport,
        financial_snapshot: FinancialSnapshot | None = None,
    ) -> CatalystTimeline:
        """为单个供应商分析催化剂时间线。"""
        import time as _time
        lang_note = "请用中文回答" if self.language == "zh" else "Answer in English"
        report_info = _extract_report_keywords(financial_snapshot)

        trend_block = ""
        if financial_snapshot and financial_snapshot.trend and financial_snapshot.trend.trend_summary:
            trend_block = f"\n- 财务趋势: {financial_snapshot.trend.trend_summary}"

        user_prompt = f"""{lang_note}

## 瓶颈环节
- 名称: {bottleneck.node_name}
- 描述: {bottleneck.node_description}
- 瓶颈得分: {bottleneck.overall_score}/10
- 关键洞察: {', '.join(bottleneck.key_insights)}

## 候选供应商
- 公司名称: {supplier.name}
- 代码: {supplier.ticker}
- 市场: {supplier.market}
- 行业: {supplier.sector}
- 描述: {supplier.description}
{f'- 研报信息: {report_info}' if report_info else ''}
{trend_block}

请识别该供应商未来6-18个月内最重要的催化剂事件（3-5个），评估每个事件的兑现时间、影响力和置信度，并给出整体紧迫度评分。

返回严格 JSON:
{{
  "events": [
    {{
      "event_type": "capacity",
      "description": "新产线投产，产能翻倍",
      "expected_date": "2025Q3",
      "confidence": 8,
      "impact_score": 7
    }}
  ],
  "urgency_score": 7,
  "investment_window": "未来1-2个季度",
  "summary": "新产能即将释放+大客户订单在手，催化剂密集"
}}"""

        prompt_len = len(self._system_prompt) + len(user_prompt)
        logger.info("[catalyst] LLM调用开始 | 公司=%s | ticker=%s | prompt长度=%d chars | timeout=%ds",
                     supplier.name, supplier.ticker, prompt_len, self.LLM_TIMEOUT)
        t0 = _time.monotonic()

        try:
            response = await asyncio.wait_for(
                self.llm.ainvoke(
                    [
                        SystemMessage(content=self._system_prompt),
                        HumanMessage(content=user_prompt),
                    ]
                ),
                timeout=self.LLM_TIMEOUT,
            )
            elapsed = _time.monotonic() - t0
            text = response.content.strip()
            logger.info("[catalyst] LLM响应到达 | 公司=%s | 耗时=%.1fs | 响应长度=%d chars",
                         supplier.name, elapsed, len(text))
            text = _strip_fences(text)
            data = json.loads(text)

            events = []
            for ev in data.get("events", []):
                events.append(CatalystEvent(
                    event_type=ev.get("event_type", ""),
                    description=ev.get("description", ""),
                    expected_date=ev.get("expected_date", ""),
                    confidence=min(10, max(0, float(ev.get("confidence", 5)))),
                    impact_score=min(10, max(0, float(ev.get("impact_score", 5)))),
                ))

            logger.info("[catalyst] 解析成功 | 公司=%s | 催化剂=%d个 | urgency=%.1f",
                         supplier.name, len(events), data.get("urgency_score", 0))
            return CatalystTimeline(
                events=events,
                urgency_score=min(10, max(0, float(data.get("urgency_score", 5)))),
                investment_window=data.get("investment_window", ""),
                summary=data.get("summary", ""),
            )
        except asyncio.TimeoutError:
            elapsed = _time.monotonic() - t0
            logger.error("[catalyst] LLM超时 | 公司=%s | 已等待=%.1fs | timeout=%ds",
                          supplier.name, elapsed, self.LLM_TIMEOUT)
            return CatalystTimeline(summary="催化剂分析超时")
        except json.JSONDecodeError as e:
            elapsed = _time.monotonic() - t0
            logger.error("[catalyst] JSON解析失败 | 公司=%s | 耗时=%.1fs | 错误=%s",
                          supplier.name, elapsed, str(e)[:200])
            return CatalystTimeline(summary="JSON解析失败")
        except Exception as e:
            elapsed = _time.monotonic() - t0
            err_type = type(e).__name__
            logger.exception("[catalyst] 未知异常 | 公司=%s | 耗时=%.1fs | 类型=%s | 错误=%s",
                              supplier.name, elapsed, err_type, str(e)[:300])
            short_msg = str(e)[:80]
            return CatalystTimeline(summary=f"{err_type}: {short_msg}")

    async def analyze_batch(
        self,
        scorecards: list[SupplierScorecard],
        bottleneck_map: dict[str, BottleneckReport],
    ) -> list[SupplierScorecard]:
        """批量分析催化剂并挂载到 scorecards 上。"""
        import time as _time
        concurrency = 4
        sem = asyncio.Semaphore(concurrency)
        batch_t0 = _time.monotonic()

        llm_name = getattr(self.llm, 'model_name', None) or getattr(self.llm, 'model', 'unknown')
        logger.info("[catalyst-batch] 开始 | 公司数=%d | 并发=%d | 模型=%s | timeout=%ds",
                     len(scorecards), concurrency, llm_name, self.LLM_TIMEOUT)

        success_count = 0
        fail_count = 0
        skip_count = 0

        async def _one(idx: int, sc: SupplierScorecard) -> None:
            nonlocal success_count, fail_count, skip_count
            bn_name = sc.bottleneck_node.split(",")[0].strip()
            bn = bottleneck_map.get(bn_name)
            if not bn:
                skip_count += 1
                logger.warning("[catalyst-batch] 跳过 #%d %s — 无匹配瓶颈节点 '%s'",
                                idx, sc.supplier.name, bn_name)
                if self._on_progress:
                    await self._on_progress(f"⊘ {sc.supplier.name}: 无匹配瓶颈节点，跳过")
                return
            sem_wait_t0 = _time.monotonic()
            async with sem:
                sem_wait = _time.monotonic() - sem_wait_t0
                if sem_wait > 0.1:
                    logger.info("[catalyst-batch] #%d %s 等待信号量 %.1fs",
                                 idx, sc.supplier.name, sem_wait)
                if self._on_progress:
                    sem_note = f" (排队{sem_wait:.0f}s)" if sem_wait > 1 else ""
                    await self._on_progress(f"▸ {sc.supplier.name}: 调用LLM中{sem_note}")
                llm_t0 = _time.monotonic()
                timeline = await self.analyze(sc.supplier, bn, sc.financial_snapshot)
                llm_elapsed = _time.monotonic() - llm_t0
                sc.catalyst = timeline
                if timeline.events:
                    success_count += 1
                else:
                    fail_count += 1
                if self._on_progress:
                    if timeline.events:
                        label = f"✓ {sc.supplier.name}: 催化剂{len(timeline.events)}个 (LLM {llm_elapsed:.0f}s)"
                    elif "超时" in (timeline.summary or ""):
                        label = f"✗ {sc.supplier.name}: LLM超时 ({llm_elapsed:.0f}s)"
                    else:
                        reason = timeline.summary or "未知错误"
                        label = f"✗ {sc.supplier.name}: {reason} ({llm_elapsed:.0f}s)"
                    await self._on_progress(label)

        tasks = [_one(i, sc) for i, sc in enumerate(scorecards)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        exception_count = sum(1 for r in results if isinstance(r, Exception))
        if exception_count:
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    logger.error("[catalyst-batch] 任务 #%d 异常: %s: %s",
                                  i, type(r).__name__, str(r)[:300])

        batch_elapsed = _time.monotonic() - batch_t0
        logger.info("[catalyst-batch] 完成 | 总耗时=%.1fs | 成功=%d | 失败=%d | 跳过=%d | 异常=%d | 平均=%.1fs/家",
                     batch_elapsed, success_count, fail_count, skip_count, exception_count,
                     batch_elapsed / max(1, len(scorecards)))
        if self._on_progress:
            await self._on_progress(
                f"📊 催化剂汇总: {success_count}成功 {fail_count}失败 {skip_count}跳过 {exception_count}异常 | "
                f"总耗时{batch_elapsed:.0f}s 平均{batch_elapsed / max(1, len(scorecards)):.0f}s/家"
            )
        return scorecards
