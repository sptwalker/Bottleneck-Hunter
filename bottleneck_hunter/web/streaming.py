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
from bottleneck_hunter.chain.financial_data import fetch_batch
from bottleneck_hunter.chain.models import MarketRegion, ScreeningResult
from bottleneck_hunter.chain.report import generate_report
from bottleneck_hunter.chain.supplier_eval import AlphaScorer, SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import SupplierSearcher
from bottleneck_hunter.llm_clients.factory import create_llm

logger = logging.getLogger(__name__)

STEP_LABELS = {
    "decompose": "正在拆解产业链...",
    "bottleneck": "正在识别瓶颈环节...",
    "supplier_search": "正在检索供应商...",
    "financial_fetch": "正在获取真实财务数据...",
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

    async def on_progress(msg):
        await queue.put(_sse("step_progress", step="decompose", message=msg, log=True))

    result = await decomposer.decompose(end_product, on_layer_start=on_layer_start, on_progress=on_progress)
    await queue.put(None)  # 结束信号
    return result


async def _run_bottleneck_with_progress(analyzer, chain, top_n, queue):
    """运行瓶颈分析并通过 queue 发送进度事件。"""
    async def on_progress(msg):
        await queue.put(_sse("step_progress", step="bottleneck", message=msg, log=True))

    result = await analyzer.analyze(chain, top_n=top_n, on_progress=on_progress)
    await queue.put(None)
    return result


async def _run_supplier_search_with_progress(searcher, reports, queue, chain_graph=None):
    """运行供应商搜索并通过 queue 发送进度事件。"""
    async def on_progress(msg):
        await queue.put(_sse("step_progress", step="supplier_search", message=msg, log=True))

    result = await searcher.search_bottlenecks(reports, on_progress=on_progress, chain_graph=chain_graph)
    await queue.put(None)
    return result


async def _run_supplier_eval_with_progress(evaluator, supplier_map, reports, queue, financial_map=None):
    """运行供应商评估并通过 queue 发送进度事件。"""
    async def on_progress(msg):
        await queue.put(_sse("step_progress", step="supplier_eval", message=msg, log=True))

    evaluator._on_progress = on_progress
    result = await evaluator.evaluate_all(supplier_map, reports, financial_map=financial_map)
    await queue.put(None)
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

        # 整体超时: 每层假设最多 8 个并发批次，每批 LLM_TIMEOUT × (MAX_RETRIES+1)
        # 5层 × 8批 × 120s × 3次 / 4并发 ≈ 3600s，再加语义去重的 120s
        max_batches_per_layer = 8
        total_timeout = (
            config.max_depth * max_batches_per_layer
            * decomposer.LLM_TIMEOUT * (decomposer.MAX_RETRIES + 1)
            // decomposer.MAX_CONCURRENCY
            + decomposer.LLM_TIMEOUT  # 语义去重
        )
        total_timeout = min(total_timeout, 1800)  # 硬上限 30 分钟
        deadline = asyncio.get_event_loop().time() + total_timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                task.cancel()
                raise TimeoutError(f"产业链拆解超时（已等待 {total_timeout}s），请尝试减少拆解层数或更换模型")
            try:
                event = await asyncio.wait_for(queue.get(), timeout=min(30.0, remaining))
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if task.done():
                    break

        chain = await task
        # 提取失败统计
        decompose_failures = chain.metadata.get("total_failures", 0)
        decompose_retries = chain.metadata.get("llm_retries", 0)
        decompose_timeouts = chain.metadata.get("llm_timeouts", 0)

        yield _sse("step_done", step="decompose", index=0, result=chain.model_dump())

        if decompose_failures > 0:
            yield _sse("step_progress", step="decompose",
                       message=f"拆解完成（{decompose_failures} 次调用失败，{decompose_retries} 次重试）")
    except Exception as e:
        logger.exception("Decompose failed")
        yield _sse("error", step="decompose", message=str(e))
        return

    # Step 2: Bottleneck（带逐节点进度）
    try:
        yield _sse("step_start", step="bottleneck", index=1, message=STEP_LABELS["bottleneck"])
        analyzer = BottleneckAnalyzer(llm=deep_llm, language=config.language)

        bn_queue = asyncio.Queue()
        bn_task = asyncio.create_task(
            _run_bottleneck_with_progress(analyzer, chain, config.top_n, bn_queue)
        )

        while True:
            try:
                event = await asyncio.wait_for(bn_queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if bn_task.done():
                    break

        reports = await bn_task

        bn_failures = analyzer._timeout_count
        bn_retries = analyzer._retry_count
        decompose_failures += bn_failures
        decompose_retries += bn_retries

        yield _sse("step_done", step="bottleneck", index=1, result=[r.model_dump() for r in reports])
        if bn_failures > 0:
            yield _sse("step_progress", step="bottleneck",
                       message=f"瓶颈分析完成（{bn_failures} 次超时放弃，{bn_retries} 次重试）")
    except Exception as e:
        logger.exception("Bottleneck analysis failed")
        yield _sse("error", step="bottleneck", message=str(e))
        return

    # Step 3: Supplier Search（带逐节点进度）
    try:
        yield _sse("step_start", step="supplier_search", index=2, message=STEP_LABELS["supplier_search"])
        searcher = SupplierSearcher(
            market=market,
            max_market_cap_yi=config.max_market_cap_yi,
            max_results=config.max_suppliers,
            language=config.language,
            llm=deep_llm,
        )

        ss_queue = asyncio.Queue()
        ss_task = asyncio.create_task(
            _run_supplier_search_with_progress(searcher, reports, ss_queue, chain_graph=chain)
        )

        while True:
            try:
                event = await asyncio.wait_for(ss_queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if ss_task.done():
                    break

        supplier_map = await ss_task
        total_suppliers = sum(len(v) for v in supplier_map.values())
        if total_suppliers == 0:
            yield _sse("step_progress", step="supplier_search",
                       message="⚠ 未找到候选供应商，请检查网络连接或调整筛选条件", log=True)
        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=2, result=flat)
    except Exception as e:
        logger.exception("Supplier search failed")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # Step 4: Financial Data Fetch（真实财务数据拉取）
    financial_map = {}
    try:
        all_suppliers = [s for sl in supplier_map.values() for s in sl]
        if all_suppliers:
            yield _sse("step_start", step="financial_fetch", index=3, message=STEP_LABELS["financial_fetch"])
            yield _sse("step_progress", step="financial_fetch",
                       message=f"正在为 {len(all_suppliers)} 家供应商拉取财务数据...", log=True)
            financial_map = await fetch_batch(all_suppliers)
            success_count = len(financial_map)
            yield _sse("step_progress", step="financial_fetch",
                       message=f"财务数据获取完成: {success_count}/{len(all_suppliers)} 家成功", log=True)
            yield _sse("step_done", step="financial_fetch", index=3,
                       result={"fetched": success_count, "total": len(all_suppliers)})
        else:
            yield _sse("step_done", step="financial_fetch", index=3, result={}, skipped=True)
    except Exception as e:
        logger.exception("Financial data fetch failed")
        yield _sse("step_progress", step="financial_fetch",
                   message=f"⚠ 财务数据获取失败: {e}，继续使用 LLM 数据评估", log=True)
        yield _sse("step_done", step="financial_fetch", index=3, result={}, error=str(e))

    # Step 5: Supplier Eval（带逐个评估进度）
    try:
        yield _sse("step_start", step="supplier_eval", index=4, message=STEP_LABELS["supplier_eval"])
        evaluator = SupplierEvaluator(llm=deep_llm, language=config.language)

        se_queue = asyncio.Queue()
        se_task = asyncio.create_task(
            _run_supplier_eval_with_progress(evaluator, supplier_map, reports, se_queue, financial_map=financial_map)
        )

        while True:
            try:
                event = await asyncio.wait_for(se_queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if se_task.done():
                    break

        scorecards = await se_task

        # Alpha 评分
        bn_score_map = {r.node_name: r.overall_score for r in reports}
        AlphaScorer.score_all(scorecards, bn_score_map)

        yield _sse("step_done", step="supplier_eval", index=4, result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Supplier evaluation failed")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    # Step 6: Cross-Validation (optional)
    try:
        if config.enable_cross_validation and config.validation_models:
            yield _sse("step_start", step="cross_validate", index=5, message=STEP_LABELS["cross_validate"])
            vm = [{"provider": m.provider, "model": m.model} for m in config.validation_models]
            logger.info(f"Cross-validation models: {vm}")
            validator = CrossValidator(validation_models=vm, language=config.language)
            validations = await validator.validate_all(scorecards)
            yield _sse("step_done", step="cross_validate", index=5, result=[v.model_dump() for v in validations])
        else:
            logger.info(f"Cross-validation skipped: enable={config.enable_cross_validation}, models={config.validation_models}")
            yield _sse("step_done", step="cross_validate", index=5, result=[], skipped=True)
    except Exception as e:
        logger.exception("Cross-validation failed")
        yield _sse("error", step="cross_validate", message=str(e))
        return

    # Determine top picks
    top_picks = []
    for cv in validations:
        if cv.consensus_score >= 5:
            top_picks.append(cv.ticker)
    if not top_picks:
        for sc in scorecards[:5]:
            if sc.overall_score >= 5:
                top_picks.append(sc.supplier.ticker)

    # Save report
    report_path = ""
    analysis_id = ""
    try:
        yield _sse("step_start", step="save", index=6, message="正在保存分析结果...")

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

        yield _sse("step_done", step="save", index=6, result={"report_path": report_path, "analysis_id": analysis_id})
    except Exception:
        logger.exception("Report generation failed")
        yield _sse("step_done", step="save", index=6, result={}, error="保存失败")

    yield _sse("complete", top_picks=top_picks, report_path=report_path, analysis_id=analysis_id,
               llm_failures=decompose_failures, llm_retries=decompose_retries, llm_timeouts=decompose_timeouts)


async def run_cross_validation(
    scorecard_dicts: list[dict],
    validation_model_configs: list,
    language: str = "zh",
) -> AsyncGenerator[dict, None]:
    """独立运行交叉验证步骤，返回 SSE 事件流。"""
    from bottleneck_hunter.chain.models import SupplierInfo, SupplierScorecard

    try:
        scorecards = []
        for sc_dict in scorecard_dicts:
            supplier_data = sc_dict.get("supplier", {})
            supplier = SupplierInfo(**supplier_data)
            scorecards.append(SupplierScorecard(
                supplier=supplier,
                bottleneck_node=sc_dict.get("bottleneck_node", ""),
                market_position=sc_dict.get("market_position", 0),
                customer_validation=sc_dict.get("customer_validation", 0),
                capacity_status=sc_dict.get("capacity_status", 0),
                financial_health=sc_dict.get("financial_health", 0),
                valuation=sc_dict.get("valuation", 0),
                overall_score=sc_dict.get("overall_score", 0),
                strengths=sc_dict.get("strengths", []),
                weaknesses=sc_dict.get("weaknesses", []),
            ))
    except Exception as e:
        logger.exception("Failed to parse scorecards for cross-validation")
        yield _sse("error", step="cross_validate", message=f"解析供应商数据失败: {e}")
        return

    if not scorecards:
        yield _sse("error", step="cross_validate", message="没有可验证的供应商")
        return

    vm = [{"provider": m.provider, "model": m.model} for m in validation_model_configs]
    if not vm:
        yield _sse("error", step="cross_validate", message="未配置验证模型")
        return

    try:
        yield _sse("step_start", step="cross_validate", index=0,
                   message=f"正在交叉验证 {len(scorecards)} 个供应商...")

        validator = CrossValidator(validation_models=vm, language=language)
        llms = validator._create_llms()

        if not llms:
            failed_names = [f"{m['provider']}/{m['model']}" for m in vm]
            yield _sse("error", step="cross_validate",
                       message=f"所有验证模型创建失败: {', '.join(failed_names)}。请检查 API Key 和模型名称。")
            return

        ok_names = [name for name, _ in llms]
        yield _sse("step_progress", step="cross_validate",
                   message=f"已连接 {len(ok_names)} 个验证模型: {', '.join(ok_names)}")

        validations = await validator.validate_all(scorecards)

        yield _sse("step_done", step="cross_validate", index=0,
                   result=[v.model_dump() for v in validations])
        yield _sse("cv_complete", result=[v.model_dump() for v in validations])
    except Exception as e:
        logger.exception("Cross-validation failed")
        yield _sse("error", step="cross_validate", message=str(e))


async def run_refresh_suppliers(
    bottleneck_dicts: list[dict],
    market_str: str,
    max_market_cap_yi: float | None,
    max_suppliers: int,
    language: str,
    provider: str,
    model: str,
) -> AsyncGenerator[dict, None]:
    """独立重新运行供应商搜索+评估，返回 SSE 事件流。"""
    from bottleneck_hunter.chain.models import BottleneckReport

    market = MARKET_MAP.get(market_str, MarketRegion.A_STOCK)

    try:
        reports = [BottleneckReport(**d) for d in bottleneck_dicts]
    except Exception as e:
        logger.exception("Failed to parse bottleneck reports for supplier refresh")
        yield _sse("error", step="supplier_search", message=f"解析瓶颈数据失败: {e}")
        return

    if not reports:
        yield _sse("error", step="supplier_search", message="没有瓶颈报告数据")
        return

    try:
        llm = create_llm(provider, model)
    except Exception as e:
        yield _sse("error", step="supplier_search", message=f"LLM 初始化失败: {e}")
        return

    # Step 1: 搜索供应商
    try:
        yield _sse("step_start", step="supplier_search", index=0,
                   message=f"正在检索 {len(reports)} 个瓶颈环节的供应商...")
        searcher = SupplierSearcher(
            market=market,
            max_market_cap_yi=max_market_cap_yi,
            max_results=max_suppliers,
            language=language,
            llm=llm,
        )

        ss_queue: asyncio.Queue = asyncio.Queue()
        ss_task = asyncio.create_task(
            _run_supplier_search_with_progress(searcher, reports, ss_queue)
        )

        while True:
            try:
                event = await asyncio.wait_for(ss_queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if ss_task.done():
                    break

        supplier_map = await ss_task
        total_suppliers = sum(len(v) for v in supplier_map.values())
        if total_suppliers == 0:
            yield _sse("step_progress", step="supplier_search",
                       message="⚠ 未找到候选供应商，请检查网络连接或调整筛选条件", log=True)
        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=0, result=flat)
    except Exception as e:
        logger.exception("Supplier search failed during refresh")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # Step 2: 评估供应商
    try:
        yield _sse("step_start", step="supplier_eval", index=1,
                   message=f"正在评估 {total_suppliers} 个供应商...")
        evaluator = SupplierEvaluator(llm=llm, language=language)

        se_queue: asyncio.Queue = asyncio.Queue()
        se_task = asyncio.create_task(
            _run_supplier_eval_with_progress(evaluator, supplier_map, reports, se_queue)
        )

        while True:
            try:
                event = await asyncio.wait_for(se_queue.get(), timeout=30.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if se_task.done():
                    break

        scorecards = await se_task
        yield _sse("step_done", step="supplier_eval", index=1,
                   result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Supplier evaluation failed during refresh")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    yield _sse("refresh_complete",
               scorecards=[sc.model_dump() for sc in scorecards])
