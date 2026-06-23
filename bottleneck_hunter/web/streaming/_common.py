"""SSE streaming shared utilities."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
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
from bottleneck_hunter.web import phase_cache

logger = logging.getLogger(__name__)

STEP_LABELS = {
    "decompose": "正在拆解产业链...",
    "bottleneck": "正在识别瓶颈环节...",
    "supplier_search": "正在检索供应商...",
    "financial_fetch": "正在获取真实财务数据...",
    "supplier_eval": "正在评估供应商...",
    "catalyst": "正在分析催化剂时间线...",
    "cross_validate": "正在多模型交叉验证...",
}

MARKET_MAP = {
    "a_stock": MarketRegion.A_STOCK,
    "us_stock": MarketRegion.US_STOCK,
    "all": MarketRegion.ALL,
}


import math


def _sanitize(obj):
    """将 NaN / Inf 替换为 None，确保 JSON 合法。"""
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _sse(event: str, **data) -> dict:
    return {"event": event, "data": json.dumps(_sanitize(data), ensure_ascii=False, default=str)}


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
