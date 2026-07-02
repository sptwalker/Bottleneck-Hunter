"""WatchlistStore 的模块级底层 helper（从 store.py 抽出，供 store.py 与各 mixin 共享）。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path


_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "watchlist.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# 规范市场枚举。历史数据曾出现裸 "us"，与 "us_stock" 无法跨表关联（导致 composite_score=0、
# scenario_valuations 写入静默失败）。所有写入路径统一经此归一化。
_MARKET_ALIASES = {"us": "us_stock", "usa": "us_stock", "cn": "cn_stock",
                   "a": "cn_stock", "hk": "hk_stock"}


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
