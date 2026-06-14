"""SSE streaming executor for the screening pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.cross_validation import CrossValidator
from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.models import MarketRegion, ScreeningResult
from bottleneck_hunter.chain.report import generate_report
from bottleneck_hunter.chain.supplier_eval import SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import SupplierSearcher
from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

STEP_LABELS = {
    "decompose": "正在拆解产业链...",
    "bottleneck": "正在识别瓶颈环节...",
    "supplier_search": "正在检索供应商...",
    "supplier_eval": "正在评估供应商...",
    "cross_validate": "正在多模型交叉验证...",
}

MARKET_MAP = {
    "a_stock": MarketRegion.A_STOCK,
    "us_stock": MarketRegion.US_STOCK,
    "all": MarketRegion.ALL,
}


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)}


async def _run_decompose_with_progress(decomposer, end_product, queue):
    """运行拆解并通过 queue 发送进度事件。"""
    async def on_layer_start(depth, max_depth, parent_count):
        await queue.put(_sse(
            "step_progress", step="decompose",
            message=f"正在拆解第 {depth}/{max_depth} 层（{parent_count} 个节点）...",
        ))

    result = await decomposer.decompose(end_product, on_layer_start=on_layer_start)
    await queue.put(None)  # 结束信号
    return result


async def stream_screening(config, store=None) -> AsyncGenerator[dict, None]:
    """Execute the pipeline step by step, yielding SSE events."""

    market = MARKET_MAP.get(config.market, MarketRegion.A_STOCK)

    try:
        deep_llm = create_llm(config.provider, config.model)
    except Exception as e:
        yield _sse("error", step="init", message=f"LLM 初始化失败: {e}")
        return

    chain = None
    reports = []
    supplier_map = {}
    scorecards = []
    validations = []

    # Step 1: Decompose（带逐层进度）
    try:
        yield _sse("step_start", step="decompose", index=0, message=STEP_LABELS["decompose"])
        decomposer = ChainDecomposer(
            llm=deep_llm,
            max_depth=config.max_depth,
            sector=config.sector,
            language=config.language,
        )

        queue = asyncio.Queue()
        task = asyncio.create_task(
            _run_decompose_with_progress(decomposer, config.end_product, queue)
        )

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if task.done():
                    break

        chain = await task
        yield _sse("step_done", step="decompose", index=0, result=chain.model_dump())
    except Exception as e:
        logger.exception("Decompose failed")
        yield _sse("error", step="decompose", message=str(e))
        return

    # Step 2: Bottleneck
    try:
        yield _sse("step_start", step="bottleneck", index=1, message=STEP_LABELS["bottleneck"])
        analyzer = BottleneckAnalyzer(llm=deep_llm, language=config.language)
        reports = await analyzer.analyze(chain, top_n=config.top_n)
        yield _sse("step_done", step="bottleneck", index=1, result=[r.model_dump() for r in reports])
    except Exception as e:
        logger.exception("Bottleneck analysis failed")
        yield _sse("error", step="bottleneck", message=str(e))
        return

    # Step 3: Supplier Search
    try:
        yield _sse("step_start", step="supplier_search", index=2, message=STEP_LABELS["supplier_search"])
        searcher = SupplierSearcher(
            market=market,
            max_market_cap_yi=config.max_market_cap_yi,
            max_results=config.max_suppliers,
            language=config.language,
            llm=deep_llm,
        )
        supplier_map = await searcher.search_bottlenecks(reports)
        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=2, result=flat)
    except Exception as e:
        logger.exception("Supplier search failed")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # Step 4: Supplier Eval
    try:
        yield _sse("step_start", step="supplier_eval", index=3, message=STEP_LABELS["supplier_eval"])
        evaluator = SupplierEvaluator(llm=deep_llm, language=config.language)
        scorecards = await evaluator.evaluate_all(supplier_map, reports)
        yield _sse("step_done", step="supplier_eval", index=3, result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Supplier evaluation failed")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    # Step 5: Cross-Validation (optional)
    try:
        if config.enable_cross_validation and config.validation_models:
            yield _sse("step_start", step="cross_validate", index=4, message=STEP_LABELS["cross_validate"])
            vm = [{"provider": m.provider, "model": m.model} for m in config.validation_models]
            validator = CrossValidator(validation_models=vm, language=config.language)
            validations = await validator.validate_all(scorecards)
            yield _sse("step_done", step="cross_validate", index=4, result=[v.model_dump() for v in validations])
        else:
            yield _sse("step_done", step="cross_validate", index=4, result=[], skipped=True)
    except Exception as e:
        logger.exception("Cross-validation failed")
        yield _sse("error", step="cross_validate", message=str(e))
        return

    # Determine top picks
    top_picks = []
    for cv in validations:
        if cv.consensus in ("pass", "concern"):
            top_picks.append(cv.ticker)
    if not top_picks:
        for sc in scorecards[:5]:
            if sc.overall_score >= 6:
                top_picks.append(sc.supplier.ticker)

    # Save report
    report_path = ""
    analysis_id = ""
    try:
        yield _sse("step_start", step="save", index=5, message="正在保存分析结果...")

        screening_result = ScreeningResult(
            sector=config.sector,
            chain=chain,
            bottleneck_reports=reports,
            supplier_scorecards=scorecards,
            cross_validations=validations,
            top_picks=top_picks,
        )
        report = generate_report(screening_result, config.language)
        output_dir = Path("output")
        output_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = config.sector.replace("/", "_").replace(" ", "")
        path = output_dir / f"{safe}_{ts}_report.md"
        path.write_text(report, encoding="utf-8")
        report_path = str(path)

        # 保存结构化数据到数据库
        if store:
            try:
                result_dict = screening_result.model_dump()
                analysis_id = store.save(config, result_dict, report_path)
                logger.info(f"分析已保存到数据库: {analysis_id}")
            except Exception:
                logger.exception("数据库保存失败")

        yield _sse("step_done", step="save", index=5, result={"report_path": report_path, "analysis_id": analysis_id})
    except Exception:
        logger.exception("Report generation failed")
        yield _sse("step_done", step="save", index=5, result={}, error="保存失败")

    yield _sse("complete", top_picks=top_picks, report_path=report_path, analysis_id=analysis_id)
