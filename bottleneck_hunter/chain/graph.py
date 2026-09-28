"""Main screening workflow using LangGraph.

Orchestrates the full pipeline:
  end product → chain decomposition → bottleneck identification
  → supplier search → supplier evaluation → fact-check gate → report

注：多模型交叉验证曾在此接线但从未生效（validator 构造后未被图引用），
其唯一的活生产者在 Web 的 legacy 端点（streaming/legacy.py），见开发日志 P1-9/P2-7。
"""

from __future__ import annotations

import logging

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.models import (
    MarketRegion,
    ScreeningResult,
)
from bottleneck_hunter.chain.picks import DEFAULT_MIN_SCORE, canonical_picks
from bottleneck_hunter.chain.supplier_eval import SupplierEvaluator
from bottleneck_hunter.chain.supplier_search import SupplierSearcher

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

async def decompose_step(state: dict, decomposer: ChainDecomposer) -> dict:
    """Step 1: Decompose the industry chain."""
    try:
        chain = await decomposer.decompose(state["end_product"])
        return {"chain": chain}
    except Exception as e:
        logger.exception("Chain decomposition failed")
        return {"error": str(e)}


async def bottleneck_step(state: dict, analyzer: BottleneckAnalyzer) -> dict:
    """Step 2: Identify bottlenecks."""
    chain = state.get("chain")
    if not chain:
        return {"error": "No chain graph available for bottleneck analysis"}

    try:
        reports = await analyzer.analyze(chain, top_n=state.get("top_n", 5))
        return {"bottleneck_reports": reports}
    except Exception as e:
        logger.exception("Bottleneck analysis failed")
        return {"error": str(e)}


async def supplier_search_step(state: dict, searcher: SupplierSearcher) -> dict:
    """Step 3: Search for suppliers for each bottleneck node."""
    bottlenecks = state.get("bottleneck_reports", [])
    if not bottlenecks:
        return {"error": "No bottleneck reports available for supplier search"}

    try:
        # 产业链对象此前没往检索层传：链内候选（_extract_chain_candidates）因此恒为空，
        # 同一次筛选在 CLI 下比 Web 少一路来源。传 state["chain"] 补上。
        supplier_map = await searcher.search_bottlenecks(bottlenecks, chain_graph=state.get("chain"))
        # Flatten for state storage
        flat_suppliers = []
        for suppliers in supplier_map.values():
            flat_suppliers.extend(suppliers)

        return {
            "supplier_map": supplier_map,
            "flat_suppliers": flat_suppliers,
        }
    except Exception as e:
        logger.exception("Supplier search failed")
        return {"error": str(e)}


async def supplier_eval_step(state: dict, evaluator: SupplierEvaluator) -> dict:
    """Step 4: Evaluate suppliers."""
    supplier_map = state.get("supplier_map", {})
    bottlenecks = state.get("bottleneck_reports", [])

    if not supplier_map or not bottlenecks:
        return {"supplier_scorecards": []}

    try:
        # 评估层此前拿不到真实财务数据，于是 _data_* 系列全空、打分退回纯 LLM 口径
        # ——同一个评分模型在 CLI 下系统地比 Web 少一层数据锚。这里自己取。
        from bottleneck_hunter.chain.financial_data import fetch_batch
        flat = [s for sup_list in supplier_map.values() for s in sup_list]
        financial_map, failed = await fetch_batch(flat)
        if failed:
            logger.warning(f"财务数据缺失 {len(failed)}/{len(flat)} 家（不影响评估，仅降级为纯 LLM 口径）")
        scorecards = await evaluator.evaluate_all(supplier_map, bottlenecks, financial_map=financial_map)
        return {"supplier_scorecards": scorecards}
    except Exception as e:
        logger.exception("Supplier evaluation failed")
        return {"error": str(e)}


async def fact_check_step(state: dict) -> dict:
    """Step 5: FactCheck gate (替代原 cross_validation)."""
    from bottleneck_hunter.chain.fact_check import apply_fact_check_to_scorecards

    scorecards = state.get("supplier_scorecards", [])
    bottleneck_reports = state.get("bottleneck_reports", [])
    if not scorecards:
        return {"fact_check_reports": []}

    try:
        reports = apply_fact_check_to_scorecards(scorecards, bottleneck_reports)
        return {"fact_check_reports": reports, "supplier_scorecards": scorecards}
    except Exception as e:
        logger.exception("FactCheck failed")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _should_stop(state: dict) -> str:
    if state.get("error"):
        return "end"
    return "continue"


def build_screening_graph(
    decomposer: ChainDecomposer,
    analyzer: BottleneckAnalyzer,
    searcher: SupplierSearcher,
    evaluator: SupplierEvaluator,
) -> StateGraph:
    """Build the full LangGraph workflow for industry chain screening."""

    async def _decompose(state: dict) -> dict:
        return await decompose_step(state, decomposer)

    async def _bottleneck(state: dict) -> dict:
        return await bottleneck_step(state, analyzer)

    async def _supplier_search(state: dict) -> dict:
        return await supplier_search_step(state, searcher)

    async def _supplier_eval(state: dict) -> dict:
        return await supplier_eval_step(state, evaluator)

    async def _fact_check(state: dict) -> dict:
        return await fact_check_step(state)

    graph = StateGraph(dict)

    graph.add_node("decompose", _decompose)
    graph.add_node("bottleneck", _bottleneck)
    graph.add_node("supplier_search", _supplier_search)
    graph.add_node("supplier_eval", _supplier_eval)
    graph.add_node("fact_check", _fact_check)

    graph.set_entry_point("decompose")

    # decompose → bottleneck → supplier_search → supplier_eval → fact_check → END
    graph.add_conditional_edges("decompose", _should_stop, {"continue": "bottleneck", "end": END})
    graph.add_conditional_edges("bottleneck", _should_stop, {"continue": "supplier_search", "end": END})
    graph.add_conditional_edges("supplier_search", _should_stop, {"continue": "supplier_eval", "end": END})
    graph.add_conditional_edges("supplier_eval", _should_stop, {"continue": "fact_check", "end": END})
    graph.add_edge("fact_check", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# High-level runner
# ---------------------------------------------------------------------------

async def run_screening(
    sector: str,
    end_product: str,
    deep_llm: BaseChatModel,
    max_depth: int = 3,
    top_n: int = 5,
    language: str = "zh",
    market: MarketRegion = MarketRegion.A_STOCK,
    max_market_cap_yi: float | None = 200,
    max_suppliers: int = 20,
) -> ScreeningResult:
    """Run the full screening pipeline.

    Args:
        sector: Sector name, e.g. "GPU/AI算力"
        end_product: Root product, e.g. "GPU"
        deep_llm: LLM for decomposition and analysis
        max_depth: Chain decomposition depth
        top_n: Number of top bottlenecks to return
        language: Output language
        market: Which market to search
        max_market_cap_yi: Max market cap filter (亿 for A-stock)
        max_suppliers: Max suppliers per bottleneck
    """
    decomposer = ChainDecomposer(llm=deep_llm, max_depth=max_depth, sector=sector, language=language,
                                 market=market)
    analyzer = BottleneckAnalyzer(llms=[(deep_llm, "unknown", "unknown")], language=language,
                                  industry=sector,
                                  market=(market.value if hasattr(market, "value") else str(market)))
    searcher = SupplierSearcher(
        market=market,
        max_market_cap_yi=max_market_cap_yi,
        max_results=max_suppliers,
        language=language,
        llm=deep_llm,
    )
    evaluator = SupplierEvaluator(llm=deep_llm, language=language)

    app = build_screening_graph(decomposer, analyzer, searcher, evaluator)

    initial_state = {
        "sector": sector,
        "end_product": end_product,
        "max_depth": max_depth,
        "top_n": top_n,
        "language": language,
        "chain": None,
        "bottleneck_reports": [],
        "supplier_map": {},
        "flat_suppliers": [],
        "supplier_scorecards": [],
        "result": None,
        "error": None,
    }

    final_state = await app.ainvoke(initial_state)

    if final_state.get("error"):
        raise RuntimeError(f"Screening failed: {final_state['error']}")

    # Determine top picks from fact_check gate（统一口径，见 chain/picks.py）
    scorecards = final_state.get("supplier_scorecards", [])
    top_picks = canonical_picks(scorecards, top_n=5, min_score=DEFAULT_MIN_SCORE)

    return ScreeningResult(
        sector=sector,
        chain=final_state.get("chain"),
        bottleneck_reports=final_state.get("bottleneck_reports", []),
        supplier_scorecards=scorecards,
        cross_validations=[],  # CLI 不再产出 CV（见模块 docstring）；字段本身仍活，Web legacy 端点会填
        top_picks=top_picks,
    )
