"""SQLite persistence for the watchlist tracking system.

WatchlistStore 按领域拆成多个 mixin（store_*.py）；本文件保留基础设施
（连接 / 过滤 / 迁移 / _parse_json_fields）并装配最终类。
schema DDL 见 store_schema.py，底层 helper 见 store_base.py。
"""

from __future__ import annotations

import json
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

from bottleneck_hunter.watchlist.store_base import _DEFAULT_DB, _get_db_lock
from contextlib import contextmanager
from bottleneck_hunter.watchlist.store_schema import (
    CREATE_TABLES as _CREATE_TABLES,
    CREATE_INDEXES as _CREATE_INDEXES,
    MIGRATIONS as _MIGRATIONS,
)
from bottleneck_hunter.watchlist.store_watchlist import _WatchlistMixin
from bottleneck_hunter.watchlist.store_market_data import _MarketDataMixin
from bottleneck_hunter.watchlist.store_budget import _BudgetMixin
from bottleneck_hunter.watchlist.store_intel import _IntelMixin
from bottleneck_hunter.watchlist.store_decision import _DecisionMixin
from bottleneck_hunter.watchlist.store_committee import _CommitteeMixin
from bottleneck_hunter.watchlist.store_simtrading import _SimTradingMixin
from bottleneck_hunter.watchlist.store_research import _ResearchMixin
from bottleneck_hunter.watchlist.store_ai_models import _AIModelsMixin
from bottleneck_hunter.watchlist.store_oplog import _OpLogMixin
from bottleneck_hunter.watchlist.store_i18n import _I18nMixin


class WatchlistStore(
    _WatchlistMixin,
    _MarketDataMixin,
    _BudgetMixin,
    _IntelMixin,
    _DecisionMixin,
    _CommitteeMixin,
    _SimTradingMixin,
    _ResearchMixin,
    _AIModelsMixin,
    _OpLogMixin,
    _I18nMixin,
):
    BLOCK_MARKER_SYSTEM = "[系统拦截]"

    BLOCK_MARKER_COMMITTEE = "[投委会否决]"

    def __init__(self, db_path: str | Path | None = None, user_id: str = ""):
        self._db_path = str(db_path or _DEFAULT_DB)
        self._user_id = user_id
        self._market = ""
        self._write_lock = _get_db_lock(self._db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()


    def for_user(self, user_id: str, *, tier_caps: dict[str, int] | None = None) -> "WatchlistStore":
        """返回绑定指定用户的 store 克隆（共享同一 DB 和写锁）。

        tier_caps: 该用户生效的分档容量 {focus, normal, track}；由 API 层按用户上限
        与全局比例配置推导（tier_limits.derive_tier_caps）后注入。省略则用默认派生。
        """
        clone = object.__new__(WatchlistStore)
        clone._db_path = self._db_path
        clone._user_id = user_id
        clone._market = self._market
        clone._write_lock = self._write_lock
        clone._tier_caps = tier_caps
        return clone


    def for_market(self, market: str) -> "WatchlistStore":
        """返回绑定指定市场的 store 克隆（共享同一 DB 和写锁）。"""
        clone = object.__new__(WatchlistStore)
        clone._db_path = self._db_path
        clone._user_id = self._user_id
        clone._market = market
        clone._write_lock = self._write_lock
        clone._tier_caps = getattr(self, "_tier_caps", None)
        return clone


    def _user_filter(self, query: str, params: tuple = (), *, table: str = "") -> tuple[str, tuple]:
        """为 SQL 查询自动追加 user_id 过滤条件。

        处理两种情况：
        1. 已有 WHERE → 在 ORDER BY/GROUP BY/LIMIT 之前插入 AND user_id = ?
        2. 无 WHERE → 在 ORDER BY/GROUP BY/LIMIT 之前插入 WHERE user_id = ?

        对于 JOIN 查询，传入 table="w" 等主表别名，生成 w.user_id = ? 避免歧义。
        """
        if not self._user_id:
            return query, params
        col = f"{table}.user_id" if table else "user_id"
        upper = query.upper()
        # G-4 安全护栏：本函数用字符串定位插入 user_id 过滤（插到 ORDER BY/GROUP BY/LIMIT 之前）。
        # 对无法保证插到正确位置的形态【显式报错】（安全失败）而非静默错插（=跨用户泄露）：
        # - UNION：始终不安全（clause 只会作用于第一个 SELECT，第二个 SELECT 无过滤）。
        # - HAVING 且无 GROUP BY：无安全插入点，clause 会追加到 HAVING 之后 → 报错。
        #   （HAVING 前有 GROUP BY 时 clause 正确插入 WHERE 段，安全，不拦截。）
        # - 子查询：字符串定位不可靠；带 table= 别名的 JOIN 由调用方保证，放宽。
        if " UNION " in upper:
            raise ValueError("_user_filter 不支持 UNION 查询，请手写带 user_id 过滤的 SQL")
        if " HAVING " in upper and " GROUP BY " not in upper:
            raise ValueError("_user_filter 不支持无 GROUP BY 的 HAVING 查询，请手写带 user_id 过滤的 SQL")
        if not table and upper.count("SELECT ") > 1:
            raise ValueError("_user_filter 不支持含子查询的 SQL，请手写带 user_id 过滤或传 table= 别名")
        has_where = " WHERE " in upper
        clause = f" AND {col} = ?" if has_where else f" WHERE {col} = ?"
        # 找到 ORDER BY / GROUP BY / LIMIT 中最早出现的关键字位置
        # 需要在 WHERE 子句之后查找（避免匹配子查询中的关键字）
        search_start = upper.find(" WHERE ") + 7 if has_where else 0
        insert_pos = len(query)
        for kw in (" ORDER BY ", " GROUP BY ", " LIMIT "):
            idx = upper.find(kw, search_start)
            if idx != -1 and idx < insert_pos:
                insert_pos = idx
        count_before = query[:insert_pos].count('?')
        query = query[:insert_pos] + clause + query[insert_pos:]
        new_params = params[:count_before] + (self._user_id,) + params[count_before:]
        return query, new_params


    def _user_insert_cols(self) -> str:
        """返回 INSERT 语句中的 user_id 列名。"""
        return ", user_id" if self._user_id else ""


    def _user_insert_vals(self) -> str:
        """返回 INSERT 语句中的 user_id 占位符。"""
        return ", ?" if self._user_id else ""


    def _user_insert_params(self) -> tuple:
        """返回 INSERT 语句中的 user_id 参数。"""
        return (self._user_id,) if self._user_id else ()


    def _market_filter(self, query: str, params: tuple = (), *, table: str = "") -> tuple[str, tuple]:
        """为 SQL 查询自动追加 market 过滤条件（与 _user_filter 平行）。

        对于 JOIN 查询，传入 table="ct" 等主表别名，生成 ct.market = ? 避免歧义。
        """
        if not self._market:
            return query, params
        col = f"{table}.market" if table else "market"
        upper = query.upper()
        has_where = " WHERE " in upper
        clause = f" AND {col} = ?" if has_where else f" WHERE {col} = ?"
        search_start = upper.find(" WHERE ") + 7 if has_where else 0
        insert_pos = len(query)
        for kw in (" ORDER BY ", " GROUP BY ", " LIMIT "):
            idx = upper.find(kw, search_start)
            if idx != -1 and idx < insert_pos:
                insert_pos = idx
        count_before = query[:insert_pos].count('?')
        query = query[:insert_pos] + clause + query[insert_pos:]
        new_params = params[:count_before] + (self._market,) + params[count_before:]
        return query, new_params


    def _market_insert_cols(self) -> str:
        return ", market" if self._market else ""


    def _market_insert_vals(self) -> str:
        return ", ?" if self._market else ""


    def _market_insert_params(self) -> tuple:
        return (self._market,) if self._market else ()


    def _filtered(self, query: str, params: tuple = (), *, table: str = "") -> tuple[str, tuple]:
        """链式 user + market 过滤。"""
        q, p = self._user_filter(query, params, table=table)
        q, p = self._market_filter(q, p, table=table)
        return q, p


    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


    @contextmanager
    def _write_conn(self):
        """获取写连接：加锁 + BEGIN IMMEDIATE 避免并发写冲突。"""
        self._write_lock.acquire()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._write_lock.release()


    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_CREATE_TABLES)
            conn.executescript(_CREATE_INDEXES)
            for sql in _MIGRATIONS:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                        logger.warning("迁移语句执行异常: %s — %s", sql[:80], e)
            # 初始化默认预算配置
            conn.execute(
                "INSERT OR IGNORE INTO budget_config(key, value) VALUES (?, ?)",
                ("daily_limit_usd", "2.00"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO budget_config(key, value) VALUES (?, ?)",
                ("monthly_limit_usd", "30.00"),
            )
            conn.commit()
        finally:
            conn.close()


    def _parse_json_fields(self, d: dict, dict_fields: tuple = (),
                           list_fields: tuple = ()) -> dict:
        for field in dict_fields:
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = {}
        for field in list_fields:
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        return d

