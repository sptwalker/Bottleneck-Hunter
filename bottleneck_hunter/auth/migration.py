"""Phase 16B 数据迁移：将现有单用户数据绑定到 admin 用户。

在 app.py lifespan 中调用 run_migration()，幂等执行。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _add_user_id_column(conn: sqlite3.Connection, table: str):
    """幂等添加 user_id 列。"""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # 列已存在


def _bind_existing_data(conn: sqlite3.Connection, table: str, admin_id: str) -> int:
    """将 user_id 为空的记录绑定到 admin 用户。返回影响行数。

    用 UPDATE OR IGNORE：budget_config 改为 (key,user_id) 复合主键后，若 admin 已有同 key 行，
    绑定全局 '' 行会撞主键——IGNORE 跳过该行（admin 自己的值保留，'' 作全局默认留存），不崩启动。
    """
    cur = conn.execute(
        f"UPDATE OR IGNORE {table} SET user_id = ? WHERE user_id = '' OR user_id IS NULL",
        (admin_id,),
    )
    return cur.rowcount


def run_migration(admin_user_id: str):
    """执行数据迁移：将所有现有数据绑定到 admin 用户。

    幂等：如果数据已有 user_id 则跳过。
    """
    if not admin_user_id:
        logger.warning("迁移跳过：未提供 admin_user_id")
        return

    # ── watchlist.db ──
    wl_db = Path("data/watchlist.db")
    if wl_db.exists():
        conn = sqlite3.connect(str(wl_db))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            wl_tables = [
                "watchlist", "market_snapshots", "news_digest", "sec_filings",
                "insider_trades", "options_activity", "earnings_reports",
                "llm_budget", "pipeline_status", "budget_config",
                "uzi_analyses", "stock_intelligence", "strategy_records",
                "macro_strategies", "strategic_plans", "tactical_plans",
                "execution_plans", "committee_reviews", "committee_consensus",
                "catalyst_tracking", "trade_feedback", "auto_reviews",
                "sim_account", "sim_positions", "sim_trades",
                "user_preferences", "experience_cards", "tuning_log",
            ]
            total = 0
            for table in wl_tables:
                try:
                    n = _bind_existing_data(conn, table, admin_user_id)
                    total += n
                except sqlite3.OperationalError:
                    pass  # 表不存在
            conn.commit()
            if total > 0:
                logger.info(f"watchlist.db 迁移完成：{total} 行数据已绑定到 admin ({admin_user_id})")
        finally:
            conn.close()

    # ── analyses.db ──
    an_db = Path("data/analyses.db")
    if an_db.exists():
        conn = sqlite3.connect(str(an_db))
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            try:
                n = _bind_existing_data(conn, "analyses", admin_user_id)
                conn.commit()
                if n > 0:
                    logger.info(f"analyses.db 迁移完成：{n} 行数据已绑定到 admin ({admin_user_id})")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
