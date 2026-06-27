"""SSE streaming — roundtable meeting function."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator

from bottleneck_hunter.web import phase_cache

from ._common import (
    logger,
    _sse,
)


async def stream_roundtable(
    *,
    analysis_id: str,
    validation_models: list[dict] | None = None,
    role_assignments: dict[str, dict[str, str]] | None = None,
    language: str = "zh",
    store=None,
) -> AsyncGenerator[dict, None]:
    """Phase 4.5: AI 投研圆桌会议。"""
    from bottleneck_hunter.chain.models import SupplierScorecard, CrossValidationReport
    from bottleneck_hunter.chain.roundtable import RoundtableMeeting
    from bottleneck_hunter.chain.meeting_data import MeetingDataFetcher

    p1 = phase_cache.get_phase(analysis_id, 1)
    p2 = phase_cache.get_phase(analysis_id, 2)
    p4 = phase_cache.get_phase(analysis_id, 4)
    _db_record = None
    if not p2 and store:
        _db_record = store.get(analysis_id)
        if _db_record and _db_record.get("result_json", {}).get("supplier_scorecards"):
            p2 = {
                "scorecards": _db_record["result_json"]["supplier_scorecards"],
                "config": {"market": _db_record.get("market", "us_stock")},
            }
            phase_cache.set_phase(analysis_id, 2, p2)
    if not p4 and store:
        if not _db_record:
            _db_record = store.get(analysis_id)
        if _db_record and _db_record.get("result_json", {}).get("cross_validations"):
            p4 = {
                "cross_validations": _db_record["result_json"]["cross_validations"],
            }
            phase_cache.set_phase(analysis_id, 4, p4)
    if not p2:
        yield _sse("meeting_error", message="Phase 2 数据未找到，请先完成筛选")
        return
    if not p4:
        yield _sse("meeting_error", message="Phase 4 数据未找到，请先完成交叉验证")
        return
    if not validation_models:
        yield _sse("meeting_error", message="未配置验证模型")
        return

    scorecards = [SupplierScorecard(**d) for d in p2["scorecards"]]
    cv_reports = [CrossValidationReport(**d) for d in p4.get("validations", [])]

    def sort_key(sc):
        if sc.final:
            return sc.final.final_score
        return sc.overall_score
    scorecards.sort(key=sort_key, reverse=True)
    scorecards = scorecards[:10]

    chain_data = p1.get("chain") if p1 else None
    bottleneck_reports = p1.get("top_reports") or (p1.get("all_reports") if p1 else None)
    analysis_config = p1.get("config") if p1 else None
    market = analysis_config.get("market", "a_stock") if analysis_config else "a_stock"

    yield _sse("meeting_status", message="正在预取最新市场数据...")

    market_data_text = ""
    try:
        fetcher = MeetingDataFetcher()
        tickers = [sc.supplier.ticker for sc in scorecards if sc.supplier.ticker]
        name_map = {sc.supplier.ticker: sc.supplier.name for sc in scorecards if sc.supplier.ticker}
        all_data = await fetcher.fetch_all(tickers, market)
        market_data_text = fetcher.format_for_briefing(all_data, name_map)
        fetched_count = sum(1 for v in all_data.values() if v)
        yield _sse("meeting_status", message=f"市场数据预取完成（{fetched_count}/{len(tickers)} 家）")
    except Exception:
        logger.exception("会前市场数据预取失败")
        yield _sse("meeting_status", message="市场数据预取失败，会议将使用已有数据继续")

    MEETING_TIMEOUT = 600  # 10 分钟全局超时

    queue: asyncio.Queue = asyncio.Queue()

    async def callback(event: str, data: dict):
        await queue.put(_sse(event, **data))

    async def run_meeting():
        try:
            meeting = RoundtableMeeting(
                validation_models=validation_models,
                language=language,
                role_assignments=role_assignments,
            )
            result = await asyncio.wait_for(
                meeting.run(
                    scorecards, cv_reports,
                    chain_data=chain_data,
                    bottleneck_reports=bottleneck_reports,
                    analysis_config=analysis_config,
                    market_data_text=market_data_text,
                    callback=callback,
                ),
                timeout=MEETING_TIMEOUT,
            )
            if store:
                try:
                    store.update_meeting_result(analysis_id, result.model_dump())
                    await queue.put(_sse("meeting_saved", completed_phases=4))
                except Exception:
                    logger.exception("会议结果保存失败")
        except asyncio.TimeoutError:
            logger.error("圆桌会议超时（%d 秒）", MEETING_TIMEOUT)
            await queue.put(_sse("meeting_error", message=f"圆桌会议超时（{MEETING_TIMEOUT}秒），已返回部分结果"))
        except Exception as e:
            logger.exception("圆桌会议执行失败")
            await queue.put(_sse("meeting_error", message=str(e)))
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_meeting())

    while True:
        item = await queue.get()
        if item is None:
            break
        yield item

    await task
