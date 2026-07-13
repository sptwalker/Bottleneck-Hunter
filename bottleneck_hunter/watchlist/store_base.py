"""WatchlistStore 的模块级底层 helper（从 store.py 抽出，供 store.py 与各 mixin 共享）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "watchlist.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    # "今天"按北京时间取（A股交易日/用户视角），避免 UTC 在北京凌晨算成昨天
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


# 规范市场枚举。历史数据曾出现裸 "us"，与 "us_stock" 无法跨表关联（导致 composite_score=0、
# scenario_valuations 写入静默失败）。所有写入路径统一经此归一化。
# 注意：A股 canonical 是 "a_stock"（全系统 MarketRegion.A_STOCK / scheduler cn_* job / fetcher 均用它），
# 故 "a"/"cn" 必须归一到 "a_stock"，不能是 "cn_stock"（后者无任何消费方，会把 A股 entry 孤立于所有 A股逻辑）。
_MARKET_ALIASES = {"us": "us_stock", "usa": "us_stock", "cn": "a_stock",
                   "a": "a_stock", "hk": "hk_stock"}


def normalize_market(market: str | None) -> str:
    m = (market or "us_stock").strip().lower()
    return _MARKET_ALIASES.get(m, m)


_DB_LOCKS: dict[str, threading.Lock] = {}


_DB_LOCKS_GUARD = threading.Lock()


def _get_db_lock(db_path: str) -> threading.Lock:
    with _DB_LOCKS_GUARD:
        if db_path not in _DB_LOCKS:
            _DB_LOCKS[db_path] = threading.Lock()
        return _DB_LOCKS[db_path]
