"""SSE streaming — legacy one-shot pipeline functions."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.catalyst import CatalystAnalyzer
from bottleneck_hunter.chain.cross_validation import CrossValidator
from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.financial_data import fetch_batch
from bottleneck_hunter.chain.models import MarketRegion, ScreeningResult
from bottleneck_hunter.chain.report import generate_report
from bottleneck_hunter.chain.smart_money import track_batch as smart_money_batch
from bottleneck_hunter.chain.supplier_eval import AlphaScorer, FinalScorer, SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import SupplierSearcher
from bottleneck_hunter.llm_clients.factory import create_llm

from ._common import (
    logger,
    STEP_LABELS,
    MARKET_MAP,
    _sanitize,
    _sse,
    _run_decompose_with_progress,
    _run_bottleneck_with_progress,
    _run_supplier_search_with_progress,
    _run_supplier_eval_with_progress,
)


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
        from bottleneck_hunter.llm_clients.factory import get_models_for_role
        bottleneck_llms = get_models_for_role("bottleneck")
        if not bottleneck_llms:
            bottleneck_llms = [(deep_llm, provider, model)]
        analyzer = BottleneckAnalyzer(llms=bottleneck_llms, language=config.language)

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
        bn_failed_nodes = analyzer.failed_nodes
        decompose_failures += bn_failures
        decompose_retries += bn_retries

        # 记录原始分析结果的层级分布
        all_layer_dist = defaultdict(int)
        for r in reports:
            all_layer_dist[r.layer] += 1
        logger.info(f"瓶颈分析原始结果: 共 {len(reports)} 个节点, 层级分布: {dict(all_layer_dist)}")

        # 按层分组，每层取 top_n 个（确保各层都有供应商覆盖）
        layer_groups: dict[int, list] = defaultdict(list)
        for r in reports:
            if r.layer > 0:
                layer_groups[r.layer].append(r)

        top_reports = []
        for layer in sorted(layer_groups.keys()):
            sorted_group = sorted(layer_groups[layer], key=lambda x: x.overall_score, reverse=True)
            top_reports.extend(sorted_group[:config.top_n])

        top_layer_dist = defaultdict(int)
        for r in top_reports:
            top_layer_dist[r.layer] += 1
        logger.info(f"top_reports 筛选后: 共 {len(top_reports)} 个节点, 层级分布: {dict(top_layer_dist)}")

        # 向前端发送层级分布信息
        dist_msg = ", ".join(f"L{k}: {v}个" for k, v in sorted(top_layer_dist.items()))
        yield _sse("step_progress", step="bottleneck",
                   message=f"瓶颈筛选完成: {dist_msg}（共 {len(top_reports)} 个节点将搜索供应商）")

        yield _sse("step_done", step="bottleneck", index=1,
                   result=[r.model_dump() for r in top_reports],
                   failed_nodes=bn_failed_nodes)
        if bn_failures > 0:
            yield _sse("step_progress", step="bottleneck",
                       message=f"瓶颈分析完成（{bn_failures} 次超时放弃，{bn_retries} 次重试，{len(bn_failed_nodes)} 个节点失败）")
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
            max_results=min(config.max_suppliers, 10),
            language=config.language,
            llm=deep_llm,
        )

        ss_queue = asyncio.Queue()
        ss_task = asyncio.create_task(
            _run_supplier_search_with_progress(searcher, top_reports, ss_queue, chain_graph=chain)
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

        # 记录供应商搜索的层级分布
        node_layer_map = {r.node_name: r.layer for r in top_reports}
        supplier_layer_dist = defaultdict(int)
        empty_nodes = []
        for node_name, suppliers in supplier_map.items():
            layer = node_layer_map.get(node_name, 0)
            supplier_layer_dist[layer] += len(suppliers)
            if not suppliers:
                empty_nodes.append(f"{node_name}(L{layer})")
        logger.info(f"供应商搜索结果: 共 {total_suppliers} 家, 层级分布: {dict(supplier_layer_dist)}")
        if empty_nodes:
            logger.warning(f"以下瓶颈节点未找到供应商: {', '.join(empty_nodes)}")

        if total_suppliers == 0:
            yield _sse("step_progress", step="supplier_search",
                       message="⚠ 未找到候选供应商，请检查网络连接或调整筛选条件", log=True)
        else:
            dist_msg = ", ".join(f"L{k}: {v}家" for k, v in sorted(supplier_layer_dist.items()) if v > 0)
            yield _sse("step_progress", step="supplier_search",
                       message=f"供应商检索完成: {dist_msg}（共 {total_suppliers} 家）", log=True)
            if empty_nodes:
                yield _sse("step_progress", step="supplier_search",
                           message=f"⚠ {len(empty_nodes)} 个节点未找到供应商: {', '.join(empty_nodes[:5])}", log=True)

        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=2, result=flat)
    except Exception as e:
        logger.exception("Supplier search failed")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # Step 4: Financial Data Fetch + Smart Money（并行拉取真实财务数据和聪明钱信号）
    financial_map = {}
    smart_money_map = {}
    failed_tickers = []
    try:
        all_suppliers = [s for sl in supplier_map.values() for s in sl]
        if all_suppliers:
            yield _sse("step_start", step="financial_fetch", index=3, message=STEP_LABELS["financial_fetch"])
            yield _sse("step_progress", step="financial_fetch",
                       message=f"正在为 {len(all_suppliers)} 家供应商拉取财务数据和聪明钱信号...", log=True)
            fin_task = fetch_batch(all_suppliers)
            sm_task = smart_money_batch(all_suppliers)
            (financial_map, fin_failed), (smart_money_map, sm_failed) = await asyncio.gather(fin_task, sm_task)
            failed_tickers = list(set(fin_failed + sm_failed))
            success_count = len(financial_map)
            sm_count = len(smart_money_map)
            msg = f"财务数据: {success_count}/{len(all_suppliers)}家，聪明钱信号: {sm_count}/{len(all_suppliers)}家"
            if failed_tickers:
                msg += f"，{len(failed_tickers)} 家数据获取失败已重试"
            yield _sse("step_progress", step="financial_fetch", message=msg, log=True)
            yield _sse("step_done", step="financial_fetch", index=3,
                       result={"fetched": success_count, "total": len(all_suppliers),
                               "smart_money": sm_count, "failed_tickers": failed_tickers})
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
            _run_supplier_eval_with_progress(evaluator, supplier_map, top_reports, se_queue, financial_map=financial_map)
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

        # 挂载聪明钱信号
        if smart_money_map:
            for sc in scorecards:
                sm = smart_money_map.get(sc.supplier.ticker)
                if sm:
                    sc.smart_money = sm

        # Alpha 评分
        bn_score_map = {r.node_name: r.overall_score for r in reports}
        AlphaScorer.score_all(scorecards, bn_score_map)

        # 记录供应商评估结果的层级分布
        sc_layer_dist = defaultdict(int)
        for sc in scorecards:
            sc_layer_dist[sc.layer] += 1
        logger.info(f"供应商评估结果: 共 {len(scorecards)} 家, 层级分布: {dict(sc_layer_dist)}")

        yield _sse("step_done", step="supplier_eval", index=4, result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Supplier evaluation failed")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    # Step 5b: 催化剂时间线分析
    try:
        yield _sse("step_start", step="catalyst", index=5, message=STEP_LABELS["catalyst"])
        yield _sse("step_progress", step="catalyst",
                   message=f"正在为 {len(scorecards)} 家供应商分析催化剂...", log=True)
        catalyst_analyzer = CatalystAnalyzer(llm=deep_llm, language=config.language)
        bn_report_map = {r.node_name: r for r in reports}
        await catalyst_analyzer.analyze_batch(scorecards, bn_report_map)

        # 催化剂分析完成后重新计算 Alpha（加入 catalyst_bonus）
        bn_score_map = {r.node_name: r.overall_score for r in reports}
        AlphaScorer.score_all(scorecards, bn_score_map)
        FinalScorer.score_all(scorecards)

        cat_count = sum(1 for sc in scorecards if sc.catalyst and sc.catalyst.events)
        yield _sse("step_progress", step="catalyst",
                   message=f"催化剂分析完成: {cat_count}/{len(scorecards)} 家有催化剂事件", log=True)
        yield _sse("step_done", step="catalyst", index=5,
                   result=[{"name": sc.supplier.name, "catalyst": sc.catalyst.model_dump() if sc.catalyst else None} for sc in scorecards])
    except Exception as e:
        logger.exception("Catalyst analysis failed")
        yield _sse("step_progress", step="catalyst",
                   message=f"⚠ 催化剂分析失败: {e}，继续后续步骤", log=True)
        yield _sse("step_done", step="catalyst", index=5, result=[], error=str(e))

    # Step 6: Cross-Validation (optional)
    try:
        if config.enable_cross_validation and config.validation_models:
            yield _sse("step_start", step="cross_validate", index=6, message=STEP_LABELS["cross_validate"])
            vm = [{"provider": m.provider, "model": m.model} for m in config.validation_models]
            logger.info(f"Cross-validation models: {vm}")
            validator = CrossValidator(validation_models=vm, language=config.language)
            validations = await validator.validate_all(scorecards)
            yield _sse("step_done", step="cross_validate", index=6, result=[v.model_dump() for v in validations])
        else:
            logger.info(f"Cross-validation skipped: enable={config.enable_cross_validation}, models={config.validation_models}")
            yield _sse("step_done", step="cross_validate", index=6, result=[], skipped=True)
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
    seq_no = 0
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
                analysis_id, seq_no = store.save(config, result_dict, report_path)
                logger.info(f"分析已保存到数据库: #{seq_no} {analysis_id}")
            except Exception:
                logger.exception("数据库保存失败")

        yield _sse("step_done", step="save", index=6, result={"report_path": report_path, "analysis_id": analysis_id, "seq_no": seq_no})
    except Exception:
        logger.exception("Report generation failed")
        yield _sse("step_done", step="save", index=6, result={}, error="保存失败")

    yield _sse("complete", top_picks=top_picks, report_path=report_path, analysis_id=analysis_id, seq_no=seq_no,
               llm_failures=decompose_failures, llm_retries=decompose_retries, llm_timeouts=decompose_timeouts,
               failed_tickers=failed_tickers)


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
                layer=sc_dict.get("layer", 0),
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

    # 记录收到的瓶颈报告层级分布
    input_layer_dist = defaultdict(int)
    for r in reports:
        input_layer_dist[r.layer] += 1
    logger.info(f"[refresh] 收到 {len(reports)} 个瓶颈报告, 层级分布: {dict(input_layer_dist)}")

    try:
        llm = create_llm(provider, model)
    except Exception as e:
        yield _sse("error", step="supplier_search", message=f"LLM 初始化失败: {e}")
        return

    # Step 1: 搜索供应商
    try:
        dist_msg = ", ".join(f"L{k}: {v}个" for k, v in sorted(input_layer_dist.items()) if k > 0)
        yield _sse("step_start", step="supplier_search", index=0,
                   message=f"正在检索 {len(reports)} 个瓶颈环节的供应商（{dist_msg}）...")
        searcher = SupplierSearcher(
            market=market,
            max_market_cap_yi=max_market_cap_yi,
            max_results=min(max_suppliers, 10),
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

        # 记录供应商搜索的层级分布
        node_layer_map = {r.node_name: r.layer for r in reports}
        supplier_layer_dist = defaultdict(int)
        empty_nodes = []
        for node_name, suppliers in supplier_map.items():
            layer = node_layer_map.get(node_name, 0)
            supplier_layer_dist[layer] += len(suppliers)
            if not suppliers:
                empty_nodes.append(f"{node_name}(L{layer})")
        logger.info(f"[refresh] 供应商搜索结果: 共 {total_suppliers} 家, 层级分布: {dict(supplier_layer_dist)}")
        if empty_nodes:
            logger.warning(f"[refresh] 以下瓶颈节点未找到供应商: {', '.join(empty_nodes)}")

        if total_suppliers == 0:
            yield _sse("step_progress", step="supplier_search",
                       message="⚠ 未找到候选供应商，请检查网络连接或调整筛选条件", log=True)
        else:
            dist_msg = ", ".join(f"L{k}: {v}家" for k, v in sorted(supplier_layer_dist.items()) if v > 0)
            yield _sse("step_progress", step="supplier_search",
                       message=f"供应商检索完成: {dist_msg}（共 {total_suppliers} 家）", log=True)
            if empty_nodes:
                yield _sse("step_progress", step="supplier_search",
                           message=f"⚠ {len(empty_nodes)} 个节点未找到供应商: {', '.join(empty_nodes[:5])}", log=True)

        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=0, result=flat)
    except Exception as e:
        logger.exception("Supplier search failed during refresh")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # Step 2: 拉取真实财务数据 + 聪明钱信号（并行）
    financial_map = {}
    smart_money_map = {}
    failed_tickers = []
    try:
        all_suppliers = [s for sl in supplier_map.values() for s in sl]
        if all_suppliers:
            yield _sse("step_progress", step="supplier_search",
                       message=f"正在为 {len(all_suppliers)} 家供应商拉取财务数据和聪明钱信号...", log=True)
            fin_task = fetch_batch(all_suppliers)
            sm_task = smart_money_batch(all_suppliers)
            (financial_map, fin_failed), (smart_money_map, sm_failed) = await asyncio.gather(fin_task, sm_task)
            failed_tickers = list(set(fin_failed + sm_failed))
            success_count = len(financial_map)
            sm_count = len(smart_money_map)
            yield _sse("step_progress", step="supplier_search",
                       message=f"财务数据: {success_count}/{len(all_suppliers)}家，聪明钱: {sm_count}/{len(all_suppliers)}家", log=True)
    except Exception as e:
        logger.exception("Financial data fetch failed during refresh")
        yield _sse("step_progress", step="supplier_search",
                   message=f"⚠ 财务数据获取失败: {e}，继续使用 LLM 数据评估", log=True)

    # Step 3: 评估供应商
    try:
        yield _sse("step_start", step="supplier_eval", index=1,
                   message=f"正在评估 {total_suppliers} 个供应商...")
        evaluator = SupplierEvaluator(llm=llm, language=language)

        se_queue: asyncio.Queue = asyncio.Queue()
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

        # 挂载聪明钱信号
        if smart_money_map:
            for sc in scorecards:
                sm = smart_money_map.get(sc.supplier.ticker)
                if sm:
                    sc.smart_money = sm

        # Alpha 预期差评分
        bn_score_map = {r.node_name: r.overall_score for r in reports}
        AlphaScorer.score_all(scorecards, bn_score_map)
        FinalScorer.score_all(scorecards)
        sc_layer_dist = defaultdict(int)
        for sc in scorecards:
            sc_layer_dist[sc.layer] += 1
        logger.info(f"[refresh] 供应商评估结果: 共 {len(scorecards)} 家, 层级分布: {dict(sc_layer_dist)}")

        yield _sse("step_done", step="supplier_eval", index=1,
                   result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Supplier evaluation failed during refresh")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    yield _sse("refresh_complete",
               scorecards=[sc.model_dump() for sc in scorecards],
               failed_tickers=failed_tickers)


async def run_retry_bottleneck(
    chain_dict: dict,
    failed_nodes: list[dict],
    provider: str,
    model: str,
    language: str,
) -> AsyncGenerator[dict, None]:
    """对失败节点使用备选引擎补充瓶颈分析，返回 SSE 事件流。"""
    from bottleneck_hunter.chain.models import ChainGraph

    try:
        chain = ChainGraph(**chain_dict)
    except Exception as e:
        logger.exception("Failed to parse chain data for bottleneck retry")
        yield _sse("error", step="bottleneck", message=f"解析产业链数据失败: {e}")
        return

    if not failed_nodes:
        yield _sse("error", step="bottleneck", message="没有需要补充分析的节点")
        return

    try:
        llm = create_llm(provider, model)
    except Exception as e:
        yield _sse("error", step="bottleneck", message=f"LLM 初始化失败: {e}")
        return

    yield _sse("step_start", step="bottleneck",
               message=f"正在使用 {provider}/{model} 补充分析 {len(failed_nodes)} 个失败节点...")

    analyzer = BottleneckAnalyzer(llms=[(llm, provider, model)], language=language)
    analyzer._failed_nodes = list(failed_nodes)

    queue = asyncio.Queue()

    async def _run():
        async def on_progress(msg):
            await queue.put(_sse("step_progress", step="bottleneck", message=msg, log=True))
        result = await analyzer.retry_failed_nodes(chain, on_progress=on_progress)
        await queue.put(None)
        return result

    task = asyncio.create_task(_run())

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=30.0)
            if event is None:
                break
            yield event
        except asyncio.TimeoutError:
            if task.done():
                break

    new_reports = await task
    still_failed = analyzer.failed_nodes

    yield _sse("step_done", step="bottleneck",
               result=[r.model_dump() for r in new_reports],
               failed_nodes=still_failed)

    summary = f"补充分析完成: {len(new_reports)} 个成功"
    if still_failed:
        summary += f", {len(still_failed)} 个仍然失败"
    yield _sse("step_progress", step="bottleneck", message=summary)
