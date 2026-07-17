"""SSE streaming — phased pipeline functions."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.catalyst import CatalystAnalyzer
from bottleneck_hunter.chain.cross_validation import CrossValidator
from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.financial_data import fetch_batch
from bottleneck_hunter.chain.models import MarketRegion
from bottleneck_hunter.chain.smart_money import track_batch as smart_money_batch
from bottleneck_hunter.chain.supplier_eval import AlphaScorer, FinalScorer, SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import SupplierSearcher
from bottleneck_hunter.llm_clients.factory import create_llm, get_llm_for_position
from bottleneck_hunter.web import phase_cache

from ._common import (
    logger,
    STEP_LABELS,
    MARKET_MAP,
    _sse,
    _sanitize,
    drain_task_queue,
    _run_decompose_with_progress,
    _run_bottleneck_with_progress,
    _run_supplier_search_with_progress,
    _run_supplier_eval_with_progress,
)


# ===========================================================================
# Phase 分步流水线
# ===========================================================================


def _primary_failed_event(stage: str, reason: str, provider: str, model: str):
    """构造 primary_failed 事件：附主模型失败原因 + 是否有备用可切换(供前端弹窗决策)。"""
    reason = reason or "调用失败"
    can_switch = False
    backup_hint = ""
    try:
        from bottleneck_hunter.llm_clients.fallback import build_fallback_candidates
        from bottleneck_hunter.auth.current_user import get_current_user_id
        cands = build_fallback_candidates(provider or "", model or "", get_current_user_id())
        can_switch = len(cands) > 0
        if cands:
            # cands 元素形如 (llm, provider, model)
            _c = cands[0]
            backup_hint = f"{_c[1]}/{_c[2]}" if len(_c) >= 3 else ""
    except Exception:  # noqa: BLE001
        pass
    return _sse("primary_failed", stage=stage, reason=reason,
                provider=provider or "", model=model or "",
                can_switch=can_switch, backup_hint=backup_hint)


async def stream_phase1(
    *,
    sector: str,
    end_product: str,
    max_depth: int = 3,
    top_n: int = 5,
    language: str = "zh",
    provider: str = "openai",
    model: str = "",
    market: str = "us_stock",
    max_market_cap_yi: float | None = 200,
    force_refresh_chain: bool = False,
    allow_fallback: bool = False,
    store=None,
) -> AsyncGenerator[dict, None]:
    """Phase 1: 产业链拆解 + 瓶颈分析。返回 ALL 瓶颈报告。

    allow_fallback: 默认 False —— 用户设了主模型时这几个环节**锁定主模型**、不走智能调度，
        主模型失败即发 primary_failed 事件让前端弹窗。为 True(用户在弹窗选「切换备用」后重发)
        则本次放行智能调度+自动替换，用备用模型跑完。
    """
    import uuid as _uuid

    analysis_id = str(_uuid.uuid4())

    try:
        if provider:
            # 用户在环节下拉选了具体模型：直接用它。allow_fallback 时套自动替换壳(弹窗后重跑用备用)。
            deep_llm = create_llm(provider, model, with_fallback=allow_fallback)
        else:
            # 「跟随顶栏配置」：默认锁定主模型(prefer_primary)、不自动替换(with_fallback=False)，
            # 主模型失败即发 primary_failed 让前端弹窗；allow_fallback(弹窗选切换后重发)则恢复调度+替换。
            deep_llm, provider, model = get_llm_for_position(
                "pipeline_decompose",
                prefer_primary=not allow_fallback,
                with_fallback=allow_fallback,
            )
            if deep_llm is None:
                raise ValueError("未配置可用的 LLM provider，请在 AI 配置中设置")
    except Exception as e:
        yield _sse("error", step="init", message=f"LLM 初始化失败: {e}")
        return
    try:
        yield _sse("step_start", step="decompose", index=0, message=STEP_LABELS["decompose"])

        # 复用已拆解的产业链：结构稳定，14 天内的缓存直接用，省 70~360 次 LLM 拆解调用。
        # force_refresh_chain=True 或缓存过旧/过浅时才重拆。
        chain = None
        if not force_refresh_chain:
            try:
                from bottleneck_hunter.chain.chain_store import ChainStore
                from bottleneck_hunter.chain.models import ChainGraph
                cached = ChainStore().get_fresh_chain(end_product, max_age_days=14,
                                                      min_depth=max_depth, sector=sector)
                if cached:
                    chain = ChainGraph(**cached["chain_json"])
                    yield _sse("step_progress", step="decompose",
                               message=f"♻ 复用已缓存产业链 v{cached['version']}"
                                       f"（{cached['created_at'][:10]}，模型 {cached.get('model_used', '?')}）"
                                       f"，省去重复拆解。如需重建请勾选「强制重建产业链」。", log=True)
                    yield _sse("step_done", step="decompose", index=0, result=chain.model_dump(), reused=True)
            except Exception:
                logger.exception("缓存产业链复用失败，回退到重新拆解")
                chain = None

        if chain is None:
            decomposer = ChainDecomposer(
                llm=deep_llm, max_depth=max_depth, sector=sector, language=language,
            )
            queue = asyncio.Queue()
            # 整体超时按拆解层数给预算（4层4200s / 5层6400s），BH_DECOMPOSE_TIMEOUT 可覆盖。
            from bottleneck_hunter.chain.decomposer import decompose_timeout_for_depth
            total_timeout = decompose_timeout_for_depth(max_depth)
            _now = asyncio.get_event_loop().time()
            # 软 deadline 早 90s：拆解器层间优雅收尾、保住已完成层，不被硬取消丢弃全部。
            soft_deadline = _now + max(total_timeout - 90, total_timeout * 0.9)
            deadline = _now + total_timeout

            task = asyncio.create_task(
                _run_decompose_with_progress(decomposer, end_product, queue, deadline=soft_deadline)
            )

            async for event in drain_task_queue(
                task, queue, deadline=deadline,
                timeout_msg=f"产业链拆解超时（已等待 {total_timeout}s），请尝试减少拆解层数或更换更快的模型",
            ):
                if isinstance(event, tuple) and event[0] == "__result__":
                    chain = event[1]
                else:
                    yield event
            if chain is None:
                raise RuntimeError("产业链拆解未返回结果")
            # 退化链保护：第 1 层没拆出任何子节点(LLM 全失败/额度不足)。
            if chain.metadata.get("decompose_failed") or not any((n.layer or 0) >= 1 for n in chain.nodes):
                _reason = chain.metadata.get("fail_reason", "") or "模型调用失败或额度不足"
                if not allow_fallback:
                    # 锁定主模型模式：主模型失败 → 发 primary_failed 让前端弹窗决策(切备用/中断)，不自动跑
                    yield _primary_failed_event("decompose", _reason, provider, model)
                    return
                raise RuntimeError(f"产业链拆解未产出任何子环节（{_reason}）；请检查所选模型可用性/余额后重试")
            yield _sse("step_done", step="decompose", index=0, result=chain.model_dump())
            if chain.metadata.get("partial"):
                yield _sse("step_progress", step="decompose",
                           message=f"⏱ 因时间预算，拆解停在第 {chain.metadata.get('stopped_at_layer', '?')} 层"
                                   f"（已返回部分结果，继续后续分析）。如需更深可减少层数或换更快模型。", log=True)
    except Exception as e:
        logger.exception("Phase1 decompose failed")
        yield _sse("error", step="decompose", message=str(e))
        return

    # ── 瓶颈 ──
    try:
        yield _sse("step_start", step="bottleneck", index=1, message=STEP_LABELS["bottleneck"])
        from bottleneck_hunter.llm_clients.factory import get_models_for_role
        # 锁定主模型：默认让瓶颈评分也直用主模型(prefer_primary，多槽交叉退化为单主模型)；
        # allow_fallback(弹窗后重跑)则恢复调度自选。
        bottleneck_llms = get_models_for_role("bottleneck", prefer_primary=not allow_fallback)
        if not bottleneck_llms:
            bottleneck_llms = [(deep_llm, provider, model)]
        analyzer = BottleneckAnalyzer(llms=bottleneck_llms, language=language, industry=sector, market=market)
        bn_queue = asyncio.Queue()
        bn_task = asyncio.create_task(
            _run_bottleneck_with_progress(analyzer, chain, top_n, bn_queue)
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

        all_reports = await bn_task
        failed_nodes = analyzer.failed_nodes

        # 按层取 top_n
        layer_groups: dict[int, list] = defaultdict(list)
        for r in all_reports:
            if r.layer > 0:
                layer_groups[r.layer].append(r)
        top_reports = []
        for layer in sorted(layer_groups.keys()):
            sorted_group = sorted(layer_groups[layer], key=lambda x: x.overall_score, reverse=True)
            top_reports.extend(sorted_group[:top_n])

        yield _sse("step_done", step="bottleneck", index=1,
                   result=[r.model_dump() for r in top_reports],
                   failed_nodes=failed_nodes)
    except Exception as e:
        logger.exception("Phase1 bottleneck failed")
        yield _sse("error", step="bottleneck", message=str(e))
        return

    # ── 缓存 + 持久化 ──
    phase1_data = {
        "chain": chain.model_dump(),
        "all_reports": [r.model_dump() for r in all_reports],
        "top_reports": [r.model_dump() for r in top_reports],
        "failed_nodes": failed_nodes,
        "config": {"sector": sector, "end_product": end_product, "max_depth": max_depth,
                   "top_n": top_n, "language": language, "provider": provider, "model": model,
                   "market": market, "max_market_cap_yi": max_market_cap_yi},
    }
    phase_cache.set_phase(analysis_id, 1, phase1_data)

    if store:
        try:
            from types import SimpleNamespace
            cfg = SimpleNamespace(
                sector=sector, end_product=end_product, provider=provider,
                model=model, market=market, max_depth=max_depth,
                top_n=top_n, max_market_cap_yi=max_market_cap_yi, language=language,
            )
            result_dict = {
                "sector": sector, "chain": chain.model_dump(),
                "bottleneck_reports": [r.model_dump() for r in all_reports],
                "supplier_scorecards": [], "cross_validations": [], "top_picks": [],
            }
            saved_id, saved_seq = store.save(cfg, result_dict)
            analysis_id = saved_id
            phase_cache.clear(phase1_data.get("_old_aid", ""))
            phase_cache.set_phase(analysis_id, 1, phase1_data)
            # 计算同赛道累计分析次数
            _run_count = store.count_by_sector(sector, end_product)
        except Exception:
            logger.exception("Phase1 保存失败")

    yield _sse("phase1_complete", analysis_id=analysis_id, seq_no=locals().get("saved_seq", 0),
               run_count=locals().get("_run_count", 0), completed_phases=1, **phase1_data)


def _load_phase1_from_db(store, analysis_id: str) -> dict | None:
    """（保留旧名，转调共享实现）Phase1 缓存未命中时从 DB 回读重建。"""
    from bottleneck_hunter.web.phase_rehydrate import load_phase1_from_db
    return load_phase1_from_db(store, analysis_id)


async def stream_phase2(
    *,
    analysis_id: str,
    per_layer_top_n: int = 8,
    layer_top_n: dict[str, int] | None = None,
    min_overall_score: float = 0.0,
    max_shortlist_count: int = 30,
    market: str = "us_stock",
    max_market_cap_yi: float | None = 200,
    max_suppliers: int = 20,
    language: str = "zh",
    provider: str = "openai",
    model: str = "",
    allow_fallback: bool = False,
    store=None,
) -> AsyncGenerator[dict, None]:
    """Phase 2: 入围筛选（搜索+财务+评估+催化剂+Alpha）。

    allow_fallback: 同 stream_phase1——默认锁定主模型、失败发 primary_failed；弹窗选切换后重发为 True。
    """
    from bottleneck_hunter.chain.models import BottleneckReport, ChainGraph

    logger.info("[stream-phase2] 启动 | provider=%s | model=%s | analysis_id=%s", provider, model, analysis_id)

    p1 = phase_cache.get_phase(analysis_id, 1)
    if not p1:
        # 缓存未命中（容器重启/TTL过期/LRU淘汰）→ 从 DB 回读重建，避免逼用户重跑 Phase 1
        p1 = _load_phase1_from_db(store, analysis_id)
    if not p1:
        yield _sse("error", step="init", message="Phase 1 数据未找到，请先运行 Phase 1")
        return

    try:
        if provider:
            deep_llm = create_llm(provider, model, with_fallback=allow_fallback)
        else:
            deep_llm, provider, model = get_llm_for_position(
                "pipeline_eval",
                prefer_primary=not allow_fallback,
                with_fallback=allow_fallback,
            )
            if deep_llm is None:
                raise ValueError("未配置可用的 LLM provider，请在 AI 配置中设置")
        llm_info = f"{provider}/{getattr(deep_llm, 'model_name', None) or getattr(deep_llm, 'model', model)}"
        logger.info("[stream-phase2] LLM创建成功 | %s | base_url=%s",
                     llm_info, getattr(getattr(deep_llm, 'client', None), '_base_url', '未知'))
    except Exception as e:
        yield _sse("error", step="init", message=f"LLM 初始化失败: {e}")
        return

    p1_config = p1.get("config", {})
    # Phase1 的 market 是该分析的权威值（Phase2 是同一分析的延续）：只要 Phase1 存了就以它为准，
    # 双向消除 Phase2 入参与 Phase1 不符导致的用错市场跑 Phase1 供应商集。
    if p1_config.get("market"):
        if p1_config["market"] != market:
            logger.info("[stream-phase2] 以 Phase1 缓存市场为准: %s (入参 %s)", p1_config["market"], market)
        market = p1_config["market"]
    market_enum = MARKET_MAP.get(market, MarketRegion.US_STOCK)  # 兜底与函数签名默认(us_stock)一致
    if max_market_cap_yi == 200 and p1_config.get("max_market_cap_yi") is not None:
        max_market_cap_yi = p1_config["max_market_cap_yi"]
    chain = ChainGraph(**p1["chain"])
    all_reports = [BottleneckReport(**r) for r in p1["all_reports"]]

    # 按层取 per_layer_top_n
    layer_groups: dict[int, list] = defaultdict(list)
    for r in all_reports:
        if r.layer > 0:
            layer_groups[r.layer].append(r)
    top_reports = []
    for layer in sorted(layer_groups.keys()):
        count = int(layer_top_n.get(str(layer), per_layer_top_n)) if layer_top_n else per_layer_top_n
        sorted_group = sorted(layer_groups[layer], key=lambda x: x.overall_score, reverse=True)
        top_reports.extend(sorted_group[:count])

    # ── 供应商搜索 ──
    yield _sse("step_progress", step="init", message=f"模型: {llm_info}", log=True)
    try:
        yield _sse("step_start", step="supplier_search", index=0, message=STEP_LABELS["supplier_search"])
        searcher = SupplierSearcher(
            market=market_enum, max_market_cap_yi=max_market_cap_yi,
            max_results=min(max_suppliers, 10), language=language, llm=deep_llm,
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
        yield _sse("step_progress", step="supplier_search",
                   message=f"供应商检索完成: 共 {total_suppliers} 家", log=True)

        flat = []
        for node_name, suppliers in supplier_map.items():
            for s in suppliers:
                flat.append({**s.model_dump(), "_bottleneck_node": node_name})
        yield _sse("step_done", step="supplier_search", index=0, result=flat)
    except Exception as e:
        logger.exception("Phase2 supplier search failed")
        yield _sse("error", step="supplier_search", message=str(e))
        return

    # ── 财务 + 聪明钱（并行） ──
    financial_map = {}
    smart_money_map = {}
    failed_tickers = []
    try:
        all_suppliers = [s for sl in supplier_map.values() for s in sl]
        if all_suppliers:
            yield _sse("step_start", step="financial_fetch", index=1, message=STEP_LABELS["financial_fetch"])
            fin_task = fetch_batch(all_suppliers, getattr(store, "_user_id", ""))
            sm_task = smart_money_batch(all_suppliers)
            (financial_map, fin_failed), (smart_money_map, sm_failed) = await asyncio.gather(fin_task, sm_task)
            failed_tickers = list(set(fin_failed + sm_failed))
            yield _sse("step_done", step="financial_fetch", index=1,
                       result={"fetched": len(financial_map), "smart_money": len(smart_money_map),
                               "failed_tickers": failed_tickers})
    except Exception as e:
        logger.exception("Phase2 financial fetch failed")
        yield _sse("step_done", step="financial_fetch", index=1, result={}, error=str(e))

    # ── 供应商评估 ──
    scorecards = []
    try:
        yield _sse("step_start", step="supplier_eval", index=2, message=STEP_LABELS["supplier_eval"])
        evaluator = SupplierEvaluator(llm=deep_llm, language=language)
        se_queue = asyncio.Queue()
        se_task = asyncio.create_task(
            _run_supplier_eval_with_progress(evaluator, supplier_map, top_reports, se_queue, financial_map=financial_map)
        )

        import time as _time
        se_start_ts = _time.monotonic()
        se_heartbeat_count = 0
        se_eval_done = 0
        se_eval_current = ""

        while True:
            try:
                event = await asyncio.wait_for(se_queue.get(), timeout=15.0)
                if event is None:
                    break
                yield event
                msg = event.get("data", "")
                if "✓" in msg or "✗" in msg:
                    se_eval_done += 1
                if "▸" in msg:
                    try:
                        import json as _json
                        d = _json.loads(msg) if isinstance(msg, str) else msg
                        se_eval_current = d.get("message", "")
                    except Exception:
                        logger.debug("SSE 消息解析跳过")
            except asyncio.TimeoutError:
                if se_task.done():
                    break
                se_heartbeat_count += 1
                elapsed = _time.monotonic() - se_start_ts
                eta_str = ""
                if se_eval_done > 0:
                    per_item = elapsed / se_eval_done
                    remaining = (total_suppliers - se_eval_done) * per_item
                    eta_str = f"，预计还需 {int(remaining)}s"
                yield _sse("step_progress", step="supplier_eval",
                           message=f"▸ 评估中... 已耗时 {int(elapsed)}s ({se_eval_done}/{total_suppliers}{eta_str})", log=False)
        scorecards = await se_task

        # 挂载聪明钱
        if smart_money_map:
            for sc in scorecards:
                sm = smart_money_map.get(sc.supplier.ticker)
                if sm:
                    sc.smart_money = sm

        # Alpha + 初步 FinalScore（用于入围筛选）
        bn_score_map = {r.node_name: r.overall_score for r in all_reports}
        AlphaScorer.score_all(scorecards, bn_score_map)
        FinalScorer.score_all(scorecards)

        yield _sse("step_done", step="supplier_eval", index=2,
                   result=[sc.model_dump() for sc in scorecards])
    except Exception as e:
        logger.exception("Phase2 supplier eval failed")
        yield _sse("error", step="supplier_eval", message=str(e))
        return

    # ── ShortlistConfig 筛选（用 final_score 排序，保住高alpha潜力股） ──
    total_before = len(scorecards)
    if min_overall_score > 0:
        scorecards = [sc for sc in scorecards
                      if (sc.final and sc.final.final_score >= min_overall_score)
                      or sc.overall_score >= min_overall_score]
    scorecards.sort(key=lambda s: s.final.final_score if s.final else s.overall_score, reverse=True)
    if max_shortlist_count and len(scorecards) > max_shortlist_count:
        scorecards = scorecards[:max_shortlist_count]

    yield _sse("step_progress", step="filter",
               message=f"筛选完成: {total_before} → {len(scorecards)} 家入围", log=True)

    # ── 催化剂（仅对入围公司） ──
    try:
        yield _sse("step_start", step="catalyst", index=3,
                   message=f"{STEP_LABELS['catalyst']}（{len(scorecards)} 家）")
        catalyst_analyzer = CatalystAnalyzer(llm=deep_llm, language=language)
        bn_report_map = {r.node_name: r for r in all_reports}

        cat_queue: asyncio.Queue = asyncio.Queue()
        cat_total = len(scorecards)
        cat_done = 0
        cat_current = ""
        import time as _time
        cat_start_ts = _time.monotonic()

        llm_name = getattr(deep_llm, 'model_name', None) or getattr(deep_llm, 'model', 'unknown')
        logger.info("[streaming-catalyst] 启动 | 入围=%d家 | 模型=%s", cat_total, llm_name)

        async def _cat_progress(msg):
            nonlocal cat_done, cat_current
            if msg.startswith("▸"):
                cat_current = msg[2:].strip()
                await cat_queue.put(_sse("step_progress", step="catalyst",
                                        message=f"▸ {cat_current} ({cat_done}/{cat_total})", log=True))
            elif msg.startswith("✓") or msg.startswith("✗"):
                cat_done += 1
                await cat_queue.put(_sse("step_progress", step="catalyst",
                                        message=f"{msg} ({cat_done}/{cat_total})", log=True))
            elif msg.startswith("⊘") or msg.startswith("📊"):
                await cat_queue.put(_sse("step_progress", step="catalyst",
                                        message=msg, log=True))

        catalyst_analyzer._on_progress = _cat_progress

        async def _run_catalyst():
            await catalyst_analyzer.analyze_batch(scorecards, bn_report_map)
            await cat_queue.put(None)

        cat_task = asyncio.create_task(_run_catalyst())
        heartbeat_count = 0
        while True:
            try:
                event = await asyncio.wait_for(cat_queue.get(), timeout=8.0)
                if event is None:
                    break
                yield event
            except asyncio.TimeoutError:
                if cat_task.done():
                    break
                heartbeat_count += 1
                elapsed = _time.monotonic() - cat_start_ts
                eta_str = ""
                if cat_done > 0:
                    per_item = elapsed / cat_done
                    remaining = (cat_total - cat_done) * per_item
                    eta_str = f"，预计还需 {int(remaining)}s"
                current_label = f" — {cat_current}" if cat_current else ""
                logger.info("[streaming-catalyst] 心跳 #%d | 已完成=%d/%d | 已耗时=%.0fs%s",
                             heartbeat_count, cat_done, cat_total, elapsed,
                             f" | 当前={cat_current}" if cat_current else "")
                yield _sse("step_progress", step="catalyst",
                           message=f"▸ 催化剂分析中{current_label} ({cat_done}/{cat_total}{eta_str})", log=False)
        await cat_task

        total_elapsed = _time.monotonic() - cat_start_ts
        logger.info("[streaming-catalyst] 完成 | 总耗时=%.1fs | 心跳数=%d", total_elapsed, heartbeat_count)

        bn_score_map = {r.node_name: r.overall_score for r in all_reports}
        AlphaScorer.score_all(scorecards, bn_score_map)

        # ── 事实核查门(FactCheck Gate) ──
        # 替代原 cross_validation 的多LLM再打分,改为确定性数据核查
        # 0 LLM 调用,~0 延迟,产出 credibility + recommendation
        # credibility 折进 quality(overall_score),REJECT 在后续 gate 拦截
        from bottleneck_hunter.chain.fact_check import apply_fact_check_to_scorecards
        fact_check_reports = apply_fact_check_to_scorecards(scorecards, all_reports)
        logger.info("[streaming-factcheck] 完成 %d 家事实核查", len(fact_check_reports))

        FinalScorer.score_all(scorecards)
        yield _sse("step_done", step="catalyst", index=3, result=[])
    except Exception as e:
        logger.exception("Phase2 catalyst failed")
        yield _sse("step_done", step="catalyst", index=3, result=[], error=str(e))

    # ── 缓存 + 持久化 ──
    # 清洗 NaN/Inf（多来自缺失财务数据的 float 字段）→ null，避免下游 JSON 序列化 500
    # （如 phase3/score 读取本缓存后返回时曾报 nan 不合法）。
    phase2_data = {
        "scorecards": _sanitize([sc.model_dump() for sc in scorecards]),
        "config": {
            "per_layer_top_n": per_layer_top_n, "min_overall_score": min_overall_score,
            "max_shortlist_count": max_shortlist_count, "market": market,
        },
        "stats": {"total_searched": total_suppliers, "after_eval": total_before, "after_filter": len(scorecards)},
        "failed_tickers": failed_tickers,
        "completed_phases": 2,
    }
    phase_cache.set_phase(analysis_id, 2, phase2_data)

    if store:
        try:
            store.update_suppliers(analysis_id, _sanitize([sc.model_dump() for sc in scorecards]),
                                   max_market_cap_yi=max_market_cap_yi)
        except Exception:
            logger.exception("Phase2 保存失败")
        # 为每个入围企业建立/更新持久化档案（含简介+评分），供观察池/决策中心按 ticker 直接调用
        try:
            store.upsert_company_archives([
                {"ticker": sc.supplier.ticker,
                 # 用企业自身 market，不用分析级 market（'all'/混合分析下才正确）
                 "market": getattr(getattr(sc.supplier, "market", None), "value", None)
                           or getattr(sc.supplier, "market", None) or market,
                 "name": sc.supplier.name or sc.supplier.ticker,
                 "scorecard": _sanitize(sc.model_dump()), "source": "phase2"}
                for sc in scorecards if getattr(sc.supplier, "ticker", "")
            ])
        except Exception:
            logger.warning("企业档案持久化失败(phase2)", exc_info=True)

    sc_count = len(phase2_data.get("scorecards", []))
    logger.info("[stream-phase2] 准备发送 phase2_complete | scorecards=%d", sc_count)
    try:
        evt = _sse("phase2_complete", analysis_id=analysis_id, **phase2_data)
        data_len = len(evt.get("data", ""))
        logger.info("[stream-phase2] phase2_complete 事件已构建 | data长度=%d chars", data_len)
        yield evt
        logger.info("[stream-phase2] phase2_complete 已yield")
    except Exception:
        logger.exception("[stream-phase2] phase2_complete 发送失败")


async def stream_phase4(
    *,
    analysis_id: str,
    top_n: int = 10,
    validation_models: list[dict] | None = None,
    language: str = "zh",
    store=None,
) -> AsyncGenerator[dict, None]:
    """Phase 4: 交叉验证 top N 公司。"""
    from bottleneck_hunter.chain.models import SupplierScorecard

    p2 = phase_cache.get_phase(analysis_id, 2)
    if not p2 and store:
        logger.info("[stream-phase4] cache miss, 尝试从数据库恢复 Phase 2 | analysis_id=%s", analysis_id)
        record = store.get(analysis_id)
        if record and record.get("result_json", {}).get("supplier_scorecards"):
            p2 = {
                "scorecards": record["result_json"]["supplier_scorecards"],
                "config": {"market": record.get("market", "us_stock")},
            }
            phase_cache.set_phase(analysis_id, 2, p2)
            logger.info("[stream-phase4] 从数据库恢复 Phase 2 成功 | scorecards=%d", len(p2["scorecards"]))
    if not p2:
        yield _sse("error", step="init", message="Phase 2 数据未找到，请先运行 Phase 2")
        return

    if not validation_models:
        yield _sse("error", step="init", message="未配置验证模型")
        return

    scorecards = [SupplierScorecard(**d) for d in p2["scorecards"]]

    # 用 final_score 排序（如果有），否则用 overall_score
    def sort_key(sc):
        if sc.final:
            return sc.final.final_score
        return sc.overall_score
    scorecards.sort(key=sort_key, reverse=True)

    # 过滤掉 REJECT(事实核查硬门)
    passed_scorecards = [sc for sc in scorecards if sc.fact_check_recommendation != "REJECT"]
    top_scorecards = passed_scorecards[:top_n]

    if not top_scorecards:
        yield _sse("error", step="fact_check_review", message="所有候选均被事实核查拦截(REJECT)")
        return

    yield _sse("step_start", step="fact_check_review", index=0,
               message=f"正在汇总 top {len(top_scorecards)} 家的事实核查结果...")

    # 事实核查结果已在 Phase 3 计算并存于 scorecard,这里只做展示汇总
    recommendations = []
    for sc in top_scorecards:
        rec_status = sc.fact_check_recommendation or "PASS"
        recommendations.append({
            "ticker": sc.supplier.ticker,
            "name": sc.supplier.name,
            "final_score": sc.final.final_score if sc and sc.final else sc.overall_score,
            "credibility": sc.final.credibility if sc.final and sc.final.credibility is not None else 10.0,
            "recommendation": rec_status,
            "pass_fail": "pass" if rec_status == "PASS" else ("concern" if rec_status == "REVIEW" else "fail"),
        })

    yield _sse("step_done", step="fact_check_review", index=0, result=recommendations)

    phase4_data = {
        "validations": [],  # 旧字段保留兼容,已废弃
        "recommendations": recommendations,
        "completed_phases": 4,
    }
    phase_cache.set_phase(analysis_id, 4, phase4_data)

    if store:
        try:
            # FactCheck 结果已在 Phase 3 保存到 scorecard，这里保存汇总的 recommendations
            store.update_cross_validations(analysis_id, recommendations)
        except Exception:
            logger.exception("Phase4 保存失败")

    yield _sse("phase4_complete", analysis_id=analysis_id, **phase4_data)
