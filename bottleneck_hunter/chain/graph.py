"""Main screening workflow using LangGraph.

Orchestrates the full pipeline:
  end product → chain decomposition → bottleneck identification
  → supplier search → supplier evaluation → cross-validation → report
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, StateGraph

from bottleneck_hunter.chain.bottleneck import BottleneckAnalyzer
from bottleneck_hunter.chain.cross_validation import CrossValidator
from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.models import (
    BottleneckReport,
    ChainGraph,
    CrossValidationReport,
    MarketRegion,
    ScreeningResult,
    SupplierInfo,
    SupplierScorecard,
)
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
        supplier_map = await searcher.search_bottlenecks(bottlenecks)
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
        scorecards = await evaluator.evaluate_all(supplier_map, bottlenecks)
        return {"supplier_scorecards": scorecards}
    except Exception as e:
        logger.exception("Supplier evaluation failed")
        return {"error": str(e)}


async def cross_validation_step(state: dict, validator: CrossValidator) -> dict:
    """Step 5: Cross-validate top suppliers."""
    scorecards = state.get("supplier_scorecards", [])
    if not scorecards:
        return {"cross_validations": []}

    try:
        reports = await validator.validate_all(scorecards)
        return {"cross_validations": reports}
    except Exception as e:
        logger.exception("Cross-validation failed")
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
    validator: CrossValidator | None = None,
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

    async def _cross_validate(state: dict) -> dict:
        if validator is None:
            return {"cross_validations": []}
        return await cross_validation_step(state, validator)

    graph = StateGraph(dict)

    graph.add_node("decompose", _decompose)
    graph.add_node("bottleneck", _bottleneck)
    graph.add_node("supplier_search", _supplier_search)
    graph.add_node("supplier_eval", _supplier_eval)
    graph.add_node("cross_validate", _cross_validate)

    graph.set_entry_point("decompose")

    # decompose → bottleneck → supplier_search → supplier_eval → cross_validate → END
    graph.add_conditional_edges("decompose", _should_stop, {"continue": "bottleneck", "end": END})
    graph.add_conditional_edges("bottleneck", _should_stop, {"continue": "supplier_search", "end": END})
    graph.add_conditional_edges("supplier_search", _should_stop, {"continue": "supplier_eval", "end": END})
    graph.add_conditional_edges("supplier_eval", _should_stop, {"continue": "cross_validate", "end": END})
    graph.add_edge("cross_validate", END)

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
    validation_models: list[dict[str, str]] | None = None,
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
        validation_models: List of {"provider", "model"} for cross-validation
    """
    decomposer = ChainDecomposer(llm=deep_llm, max_depth=max_depth, sector=sector, language=language)
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
    validator = CrossValidator(validation_models=validation_models or [], language=language) if validation_models else None

    app = build_screening_graph(decomposer, analyzer, searcher, evaluator, validator)

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
        "cross_validations": [],
        "result": None,
        "error": None,
    }

    final_state = await app.ainvoke(initial_state)

    if final_state.get("error"):
        raise RuntimeError(f"Screening failed: {final_state['error']}")

    # Determine top picks from cross-validation
    top_picks = []
    cross_validations = final_state.get("cross_validations", [])
    for cv in cross_validations:
        if cv.consensus_score >= 5:
            top_picks.append(cv.ticker)

    # Fallback: if no cross-validation, pick from scorecards
    if not top_picks:
        for sc in final_state.get("supplier_scorecards", [])[:5]:
            if sc.overall_score >= 5:
                top_picks.append(sc.supplier.ticker)

    return ScreeningResult(
        sector=sector,
        chain=final_state.get("chain"),
        bottleneck_reports=final_state.get("bottleneck_reports", []),
        supplier_scorecards=final_state.get("supplier_scorecards", []),
        cross_validations=cross_validations,
        top_picks=top_picks,
    )
