"""Phase 缓存未命中时从 DB 回读重建（容器重启/TTL过期/LRU淘汰后仍可续跑）。

Phase1/2 完成即持久化到 AnalysisStore(result_json)，故内存缓存丢失后可据此恢复，
不必逼用户重跑。Phase3/4 是 Phase2 的派生计算，重跑上游即可，无需单独回读。

只依赖 phase_cache，不引入 store/模型的重依赖，供 api.py 与 streaming/*.py 共用。
"""

from __future__ import annotations

import logging

from bottleneck_hunter.web import phase_cache

logger = logging.getLogger(__name__)


def load_phase1_from_db(store, analysis_id: str) -> dict | None:
    """Phase1 缓存未命中 → 从 DB 的 chain+bottleneck_reports 重建并写回缓存。"""
    if store is None or not analysis_id:
        return None
    try:
        record = store.get(analysis_id)
    except Exception:  # noqa: BLE001
        logger.exception("从 DB 回读 Phase1 失败 id=%s", analysis_id)
        return None
    if not record:
        return None
    result = record.get("result_json", {}) or {}
    chain = result.get("chain")
    all_reports = result.get("bottleneck_reports", [])
    if not chain or not all_reports:
        return None
    top_n = record.get("top_n") or 5
    top_reports = sorted(all_reports, key=lambda r: r.get("overall_score", 0), reverse=True)[:top_n]
    phase1_data = {
        "chain": chain,
        "all_reports": all_reports,
        "top_reports": top_reports,
        "config": {"market": record.get("market"), "max_market_cap_yi": record.get("max_market_cap_yi"),
                   "sector": record.get("sector"), "end_product": record.get("end_product")},
    }
    phase_cache.set_phase(analysis_id, 1, phase1_data)
    logger.info("Phase1 缓存未命中，已从 DB 回读重建 id=%s", analysis_id)
    return phase1_data


def load_phase2_from_db(store, analysis_id: str) -> dict | None:
    """Phase2 缓存未命中 → 从 DB 的 supplier_scorecards 重建并写回缓存。"""
    if store is None or not analysis_id:
        return None
    try:
        record = store.get(analysis_id)
    except Exception:  # noqa: BLE001
        logger.exception("从 DB 回读 Phase2 失败 id=%s", analysis_id)
        return None
    if not record:
        return None
    scorecards = (record.get("result_json") or {}).get("supplier_scorecards")
    if not scorecards:
        return None
    n = len(scorecards)
    phase2_data = {
        "scorecards": scorecards,
        "config": {"market": record.get("market", "us_stock"),
                   "max_market_cap_yi": record.get("max_market_cap_yi")},
        "stats": {"total_searched": n, "after_eval": n, "after_filter": n},
    }
    phase_cache.set_phase(analysis_id, 2, phase2_data)
    logger.info("Phase2 缓存未命中，已从 DB 回读重建 id=%s scorecards=%d", analysis_id, n)
    return phase2_data
