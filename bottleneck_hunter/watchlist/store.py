"""SQLite persistence for the watchlist tracking system.

WatchlistStore 按领域拆成多个 mixin（store_*.py）；本文件保留基础设施
（连接 / 过滤 / 迁移 / _parse_json_fields）并装配最终类。
schema DDL 见 store_schema.py，底层 helper 见 store_base.py。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

from contextlib import contextmanager

from bottleneck_hunter.watchlist.store_ai_models import _AIModelsMixin
from bottleneck_hunter.watchlist.store_base import _DEFAULT_DB, _get_db_lock
from bottleneck_hunter.watchlist.store_budget import _BudgetMixin
from bottleneck_hunter.watchlist.store_committee import _CommitteeMixin
from bottleneck_hunter.watchlist.store_decision import _DecisionMixin
from bottleneck_hunter.watchlist.store_i18n import _I18nMixin
from bottleneck_hunter.watchlist.store_intel import _IntelMixin
from bottleneck_hunter.watchlist.store_market_data import _MarketDataMixin
from bottleneck_hunter.watchlist.store_oplog import _OpLogMixin
from bottleneck_hunter.watchlist.store_research import _ResearchMixin
from bottleneck_hunter.watchlist.store_schema import (
    CREATE_INDEXES as _CREATE_INDEXES,
)
from bottleneck_hunter.watchlist.store_schema import (
    CREATE_TABLES as _CREATE_TABLES,
)
from bottleneck_hunter.watchlist.store_schema import (
    EXPERIENCE_CARDS_FTS_TABLE as _EC_FTS_TABLE,
)
from bottleneck_hunter.watchlist.store_schema import (
    EXPERIENCE_CARDS_FTS_TRIGGERS as _EC_FTS_TRIGGERS,
)
from bottleneck_hunter.watchlist.store_schema import (
    MIGRATIONS as _MIGRATIONS,
)
from bottleneck_hunter.watchlist.store_simtrading import _SimTradingMixin
from bottleneck_hunter.watchlist.store_vip_projection import _VipProjectionMixin
from bottleneck_hunter.watchlist.store_watchlist import _WatchlistMixin


class WatchlistStore(
    _WatchlistMixin,
    _MarketDataMixin,
    _BudgetMixin,
    _IntelMixin,
    _DecisionMixin,
    _CommitteeMixin,
    _SimTradingMixin,
    _VipProjectionMixin,
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


    def for_user(self, user_id: str, *, tier_caps: dict[str, int] | None = None) -> WatchlistStore:
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


    def for_market(self, market: str) -> WatchlistStore:
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
        # - 顶层裸 OR：clause 追加成 ` AND col=?`，SQL 中 AND 优先级高于 OR，会只绑定最后一个 OR 分支
        #   → 其余 OR 分支跨用户泄露。因零误报的括号平衡检测成本高、全库仅 get_relevant_cards 一处，
        #   这里【约定】调用方把 OR 组整体括号包裹（见 store_research.get_relevant_cards），不做自动拦截。
        #   升级路径＝扫描 WHERE..插入点间括号深度、深度0 遇 OR 即 raise（同 UNION/HAVING 的安全失败风格）。
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
            self._migrate_budget_config_pk(conn)
            self._migrate_shared_company_profiles(conn)
            self._migrate_watchlist_drop_global_unique(conn)
            self._migrate_catalyst_market_from_entry(conn)
            self._migrate_market_labels_from_source(conn)
            self._migrate_normalize_astock_tickers(conn)
            self._migrate_normalize_us_exchange_suffix(conn)
            self._migrate_sim_account_per_account(conn)
            self._migrate_vip_imports_account_ref(conn)
            self._migrate_vip_derivative_terms_account_ref(conn)
            self._migrate_flag_indicative_derivative_terms(conn)
            self._migrate_vip_projections_lot_key(conn)
            self._migrate_vip_reports_account_ref(conn)
            self._migrate_chat_sessions_account_ref(conn)
            self._migrate_chat_messages_account_ref(conn)
            self._migrate_experience_cards_widen_scope(conn)
            self._migrate_experience_cards_fts(conn)
            self._migrate_focus_reports_history(conn)
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

    def _migrate_budget_config_pk(self, conn) -> None:
        """budget_config 主键从「仅 key」重建为「(key, user_id)」复合主键。

        旧 schema 下 INSERT OR REPLACE 会让不同用户的同名 key（如 daily_limit_usd）互相覆盖，
        破坏每用户预算隔离。此处幂等重建（已是复合主键则跳过）。预算数据可再生，重建低风险。
        """
        try:
            info = conn.execute("PRAGMA table_info(budget_config)").fetchall()
            if not info:
                return
            pk_cols = [r["name"] for r in info if r["pk"]]
            if "user_id" in pk_cols:  # 已是复合主键
                return
            conn.execute(
                "CREATE TABLE IF NOT EXISTS budget_config_new "
                "(key TEXT NOT NULL, value TEXT NOT NULL, user_id TEXT DEFAULT '', "
                " PRIMARY KEY (key, user_id))"
            )
            has_uid = any(r["name"] == "user_id" for r in info)
            src = "key, value, COALESCE(user_id,'')" if has_uid else "key, value, ''"
            conn.execute(f"INSERT OR IGNORE INTO budget_config_new(key, value, user_id) SELECT {src} FROM budget_config")
            conn.execute("DROP TABLE budget_config")
            conn.execute("ALTER TABLE budget_config_new RENAME TO budget_config")
            logger.info("budget_config 主键已重建为 (key, user_id)，修复跨用户预算覆盖")
        except sqlite3.OperationalError as e:
            logger.warning("budget_config 主键重建失败（可忽略，退回旧行为）: %s", e)


    def _migrate_focus_reports_history(self, conn) -> None:
        """focus_reports 从「每键一行(主键 ticker,user_id,market)」重建为「加自增 id、每键留多份历史」。

        旧 schema 是 INSERT OR REPLACE 覆盖式，只留最新一份、无版本。要环形留存最近 N 份，须先给表
        加自增 id、去掉三列复合主键(降为非唯一索引)。列级主键无法 ALTER，只能重建表。幂等：已有 id
        列则跳过。研报是用户自传数据，重建全程搬迁所有旧行(各获新 id)，不能丢。CREATE 会独立自动提交，
        若上次迁移崩在 DROP/RENAME 之前会残留一张空 _new 孤儿表——故重建前先 DROP 掉它保证可重入，
        否则重启时 CREATE 撞名报错被 except 静默吞掉、迁移永久卡死；旧行搬迁中途失败随事务回滚不丢数据。
        """
        try:
            info = conn.execute("PRAGMA table_info(focus_reports)").fetchall()
            if not info:
                return
            if any(r["name"] == "id" for r in info):  # 已重建
                return
            conn.execute("DROP TABLE IF EXISTS focus_reports_new")  # 清上次崩溃残留的孤儿表，保证可重入
            conn.execute("""
                CREATE TABLE focus_reports_new (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker      TEXT NOT NULL,
                    filename    TEXT DEFAULT '',
                    report_text TEXT DEFAULT '',
                    char_len    INTEGER DEFAULT 0,
                    uploaded_at TEXT,
                    user_id     TEXT DEFAULT '',
                    market      TEXT DEFAULT ''
                )
            """)
            conn.execute(
                "INSERT INTO focus_reports_new"
                "(ticker, filename, report_text, char_len, uploaded_at, user_id, market) "
                "SELECT ticker, filename, report_text, char_len, uploaded_at, user_id, market "
                "FROM focus_reports"
            )
            conn.execute("DROP TABLE focus_reports")
            conn.execute("ALTER TABLE focus_reports_new RENAME TO focus_reports")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_focus_reports_key "
                         "ON focus_reports(ticker, user_id, market, uploaded_at DESC)")
            logger.info("focus_reports 已重建：加自增 id、去三列复合主键，支持每键留最近多份历史")
        except sqlite3.OperationalError as e:
            logger.warning("focus_reports 历史留存重建失败（可忽略，退回旧行为）: %s", e)


    def _migrate_watchlist_drop_global_unique(self, conn) -> None:
        """去掉 watchlist 旧的全局 `ticker UNIQUE`，改为 (user_id, ticker) 复合唯一。

        旧基表列级 `ticker TEXT NOT NULL UNIQUE` 是全局唯一 → 两个用户无法观察同一支票
        (第二个 INSERT 撞 UNIQUE 失败)，破坏多用户 + 与公共信息层"同票全员共享数据"相悖。
        列级 UNIQUE 无法 ALTER 掉，只能重建表。幂等：已无全局 UNIQUE 则跳过。观察池是用户核心
        数据——重建全程搬迁所有列、保留 user_id，且外层 _init_db 在单事务里，失败回滚不丢数据。
        """
        try:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
            if not row or not row["sql"]:
                return
            ddl = row["sql"]
            # 仅 ticker 列带列级 UNIQUE；已重建过(无该约束)则跳过
            if "UNIQUE" not in ddl.upper():
                return

            cols = [r["name"] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
            has_uid = "user_id" in cols
            # 目标表：与 store_schema 基表一致(无全局 UNIQUE、含 user_id)
            conn.execute("""
                CREATE TABLE watchlist_new (
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, company_name TEXT NOT NULL,
                    company_name_cn TEXT DEFAULT '', market TEXT DEFAULT 'us_stock',
                    tier TEXT NOT NULL CHECK(tier IN ('focus','normal','track')),
                    tier_rank INTEGER DEFAULT 0, composite_score REAL DEFAULT 0.0,
                    source TEXT DEFAULT 'manual', source_analysis_id TEXT, sector TEXT DEFAULT '',
                    bottleneck_node TEXT DEFAULT '', added_at TEXT NOT NULL, updated_at TEXT,
                    notes TEXT DEFAULT '', is_active INTEGER DEFAULT 1, user_id TEXT DEFAULT ''
                )
            """)
            base = ("id, ticker, company_name, company_name_cn, market, tier, tier_rank, "
                    "composite_score, source, source_analysis_id, sector, bottleneck_node, "
                    "added_at, updated_at, notes, is_active")
            uid_sel = "COALESCE(user_id,'')" if has_uid else "''"
            conn.execute(f"INSERT INTO watchlist_new({base}, user_id) SELECT {base}, {uid_sel} FROM watchlist")
            conn.execute("DROP TABLE watchlist")
            conn.execute("ALTER TABLE watchlist_new RENAME TO watchlist")
            # 重建索引（含 (user_id,ticker) 复合唯一 → 每用户内唯一，跨用户可共享同票）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_tier ON watchlist(tier, composite_score DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_user_ticker ON watchlist(user_id, ticker)")
            logger.info("watchlist 已重建：去掉全局 ticker UNIQUE，改 (user_id,ticker) 复合唯一，多用户可共享同票")
        except sqlite3.OperationalError as e:
            logger.warning("watchlist 去全局 UNIQUE 重建失败（可忽略，退回旧行为）: %s", e)


    def _migrate_catalyst_market_from_entry(self, conn) -> None:
        """按关联 watchlist entry 的真实 market 回填纠正 catalyst_tracking.market。

        catalyst_tracking.market 列是后加的(ALTER ADD COLUMN DEFAULT 'us_stock')——列存在之前
        建的 A股催化剂被默认打成 us_stock，泄漏到美股视图看似"混在一起"。以 entry.market 为准
        (加票时定的、可靠)纠错。孤儿催化剂(entry 已删)不动。幂等(只改 market≠entry.market 的行)。
        """
        try:
            n = conn.execute("""
                UPDATE catalyst_tracking
                SET market = (SELECT w.market FROM watchlist w WHERE w.id = catalyst_tracking.entry_id)
                WHERE entry_id IN (SELECT id FROM watchlist)
                  AND market IS NOT (SELECT w.market FROM watchlist w WHERE w.id = catalyst_tracking.entry_id)
            """).rowcount
            if n:
                logger.info("catalyst_tracking.market 按 entry 真实市场纠正 %d 行（修历史错标混市场）", n)
        except sqlite3.OperationalError as e:
            logger.warning("catalyst 市场纠正迁移失败（可忽略）: %s", e)


    def _migrate_market_labels_from_source(self, conn) -> None:
        """全面纠正历史错标市场：market 列后加(ALTER DEFAULT 'us_stock')，列存在前建的 A股行被误打
        us_stock，泄漏进美股视图。按可靠来源回填(与 catalyst 同法)，依赖顺序：先纠父表再纠子表。

        - entry_id → watchlist.market（entry 加票时定的，权威）：investment_theses / scenario_valuations /
          tactical_plans / execution_plans / sim_positions / sim_trades。
        - 无 entry_id 或孤儿：ticker 数字判 A股（6位数字/.SZ/.SS，与 options_pipeline 同规则）。
        - 2 跳：committee_reviews/consensus ← execution_plans；auto_reviews ← sim_trades；sim_fund_ops ← sim_account。
        全部幂等(WHERE market != 推导值)。孤儿(源缺失)保持原值。市场标签纠正、不删数据，低风险。
        """
        A_TICKER = "(ticker GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]*' OR ticker LIKE '%.SZ' OR ticker LIKE '%.SS')"
        total = 0
        try:
            # 1) entry_id → watchlist.market（有 entry_id 列的表）
            for t in ("investment_theses", "scenario_valuations", "tactical_plans",
                      "execution_plans", "sim_positions", "sim_trades"):
                try:
                    n = conn.execute(f"""
                        UPDATE {t} SET market = (SELECT w.market FROM watchlist w WHERE w.id = {t}.entry_id)
                        WHERE entry_id IN (SELECT id FROM watchlist)
                          AND market IS NOT (SELECT w.market FROM watchlist w WHERE w.id = {t}.entry_id)
                    """).rowcount
                    total += n or 0
                except sqlite3.OperationalError:
                    pass
            # 2) ticker 兜底（entry_id 空/孤儿，但 ticker 判得出 A股）——只把误标 us_stock 的 A股票改回
            for t in ("tactical_plans", "execution_plans", "sim_positions", "sim_trades",
                      "trade_feedback", "auto_reviews"):
                try:
                    n = conn.execute(
                        f"UPDATE {t} SET market='a_stock' WHERE market='us_stock' AND {A_TICKER}"
                    ).rowcount
                    total += n or 0
                except sqlite3.OperationalError:
                    pass
            # 3) 2 跳：委员评审/共识 ← execution_plans（已在步骤1/2 纠正）
            for t in ("committee_reviews", "committee_consensus"):
                try:
                    n = conn.execute(f"""
                        UPDATE {t} SET market = (SELECT ep.market FROM execution_plans ep WHERE ep.id = {t}.execution_plan_id)
                        WHERE execution_plan_id IN (SELECT id FROM execution_plans)
                          AND market IS NOT (SELECT ep.market FROM execution_plans ep WHERE ep.id = {t}.execution_plan_id)
                    """).rowcount
                    total += n or 0
                except sqlite3.OperationalError:
                    pass
            # 4) auto_reviews ← sim_trades（sim_trade_id）
            try:
                n = conn.execute("""
                    UPDATE auto_reviews SET market = (SELECT st.market FROM sim_trades st WHERE st.id = auto_reviews.sim_trade_id)
                    WHERE sim_trade_id IN (SELECT id FROM sim_trades)
                      AND market IS NOT (SELECT st.market FROM sim_trades st WHERE st.id = auto_reviews.sim_trade_id)
                """).rowcount
                total += n or 0
            except sqlite3.OperationalError:
                pass
            # 5) sim_fund_ops ← sim_account（account_id）
            try:
                n = conn.execute("""
                    UPDATE sim_fund_ops SET market = (SELECT sa.market FROM sim_account sa WHERE sa.id = sim_fund_ops.account_id)
                    WHERE account_id IN (SELECT id FROM sim_account)
                      AND market IS NOT (SELECT sa.market FROM sim_account sa WHERE sa.id = sim_fund_ops.account_id)
                """).rowcount
                total += n or 0
            except sqlite3.OperationalError:
                pass
            if total:
                logger.info("历史市场错标全面纠正 %d 行（theses/plans/sim/committee/reviews 等，修 A股泄漏进美股视图）", total)
        except sqlite3.OperationalError as e:
            logger.warning("历史市场标签纠正迁移失败（可忽略）: %s", e)


    def _migrate_normalize_astock_tickers(self, conn) -> None:
        """把历史 A股 ticker 归一为 canonical(.SS/.SZ/.BJ)，根治 .SH 与观察池 .SS 精确匹配失败。

        普通 ticker 列表用 SQL 批量改（.SH→.SS）；strategic_plans.stock_selection 里嵌 JSON 的
        holding ticker 用 Python 读出→normalize→写回。只动 A股(.SH 后缀/裸6位)，美股不动。幂等。
        """
        from bottleneck_hunter.watchlist.store_base import normalize_ticker
        try:
            # 1) 普通 ticker 列：上交所 .SH → .SS（其它后缀本已 canonical；裸码留给写入口/下次刷新归一）
            plain = ("watchlist", "execution_plans", "tactical_plans", "sim_positions",
                     "sim_trades", "catalyst_tracking", "market_snapshots",
                     "investment_theses", "scenario_valuations", "auto_reviews", "trade_feedback")
            total = 0
            for t in plain:
                try:
                    n = conn.execute(
                        f"UPDATE {t} SET ticker = substr(ticker,1,length(ticker)-3) || '.SS' "
                        f"WHERE ticker LIKE '%.SH'"
                    ).rowcount
                    total += n or 0
                except sqlite3.OperationalError:
                    pass
            # 2) strategic_plans.stock_selection 内嵌 holdings：JSON 读出→归一→写回
            try:
                rows = conn.execute("SELECT id, stock_selection FROM strategic_plans").fetchall()
                for r in rows:
                    raw = r["stock_selection"]
                    if not raw or ".SH" not in str(raw):
                        continue
                    try:
                        ss = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    changed = False
                    for bucket in ("core_holdings", "tactical_holdings"):
                        for h in (ss.get(bucket) or []):
                            if isinstance(h, dict) and h.get("ticker"):
                                nt = normalize_ticker(h["ticker"])
                                if nt != h["ticker"]:
                                    h["ticker"] = nt; changed = True
                    if isinstance(ss.get("watchlist_only"), list):
                        nw = [normalize_ticker(x) for x in ss["watchlist_only"]]
                        if nw != ss["watchlist_only"]:
                            ss["watchlist_only"] = nw; changed = True
                    if changed:
                        conn.execute("UPDATE strategic_plans SET stock_selection = ? WHERE id = ?",
                                     (json.dumps(ss, ensure_ascii=False), r["id"]))
                        total += 1
            except sqlite3.OperationalError:
                pass
            if total:
                logger.info("A股 ticker 归一 %d 处（.SH→.SS，统一 canonical，修 L2/L3/L4 连接漏配）", total)
        except sqlite3.OperationalError as e:
            logger.warning("A股 ticker 归一迁移失败（可忽略）: %s", e)


    def _migrate_normalize_us_exchange_suffix(self, conn) -> None:
        """剥除历史美股 ticker 的交易所后缀 .US（如 MRVL.US→MRVL）。

        反向分析/EOD 源偶带 .US 后缀 → yfinance/finnhub 无法解析(403/超时)，行情/基本面恒空。
        只动带 .US 的美股票；A股(.SS/.SZ/.BJ)不受影响。幂等（无 .US 则 0 行）。
        """
        plain = ("watchlist", "execution_plans", "tactical_plans", "sim_positions",
                 "sim_trades", "catalyst_tracking", "market_snapshots",
                 "investment_theses", "scenario_valuations", "auto_reviews", "trade_feedback")
        total = 0
        for t in plain:
            try:
                n = conn.execute(
                    f"UPDATE {t} SET ticker = substr(ticker,1,length(ticker)-3) "
                    f"WHERE ticker LIKE '%.US' AND length(ticker) > 3"
                ).rowcount
                total += n or 0
            except sqlite3.OperationalError:
                pass
        if total:
            logger.info("美股 ticker 剥除交易所后缀 %d 处（.US→裸码，修 yfinance/finnhub 解析失败）", total)


    def _migrate_shared_company_profiles(self, conn) -> None:
        """阶段2 公共信息层：company_profiles(PK 含 user_id, 每用户一份) 折叠进共享桶 __shared__。

        每 ticker 只保留 fetched_at 最新的一行 → 删其余 → 该行 user_id 改 __shared__。
        PK 安全(折叠后每 ticker 仅一行)、幂等(已折叠则无非共享行, 均为 no-op)。基本面可再拉, 低风险。
        """
        try:
            if not conn.execute("SELECT 1 FROM company_profiles WHERE user_id!='__shared__' LIMIT 1").fetchone():
                return  # 已折叠或无数据
            # 每 ticker 保留 fetched_at 最新的一条(rowid 最大做次级去重), 删掉其余
            conn.execute("""
                DELETE FROM company_profiles
                WHERE rowid NOT IN (
                    SELECT rowid FROM company_profiles cp
                    WHERE fetched_at = (SELECT MAX(fetched_at) FROM company_profiles WHERE ticker = cp.ticker)
                    GROUP BY ticker HAVING rowid = MAX(rowid)
                )
            """)
            # 存活行重贴共享标签(若某 ticker 已有 __shared__ 行且又留了个非共享的, 上一步已只留一行, 安全)
            conn.execute("UPDATE company_profiles SET user_id='__shared__' WHERE user_id!='__shared__'")
            logger.info("company_profiles 已折叠进共享桶 __shared__（每 ticker 保留最新一条）")
        except sqlite3.OperationalError as e:
            logger.warning("company_profiles 共享折叠失败（可忽略）: %s", e)


    def _table_cols(self, conn, table: str) -> set[str]:
        try:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.OperationalError:
            return set()


    def _table_sql(self, conn, table: str) -> str:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return (row["sql"] or "") if row else ""


    # ponytail: 已删除 _migrate_purge_empty_account_ref —— 它无条件删所有 account_ref='' 的
    # sim_account+sim_*，而决策中心正用 account_ref='' 作合法业务键，每次重启都清空决策模拟账户
    # （见 memory project_dc_sim_account_decoupled_from_vip）。移除迁移即根因修复。


    def _migrate_sim_account_per_account(self, conn) -> None:
        """sim_account 从 market 单槽重建为 per-account 槽。"""
        try:
            cols = self._table_cols(conn, "sim_account")
            if not cols:
                return
            ddl = self._table_sql(conn, "sim_account")
            if "account_ref" in cols and "UNIQUE(user_id, market, account_ref)" in ddl:
                return
            has_account_ref = "account_ref" in cols
            peak_expr = "COALESCE(peak_equity, 0)" if "peak_equity" in cols else "0"
            account_ref_expr = "COALESCE(account_ref, '')" if has_account_ref else "''"
            conn.execute("""
                CREATE TABLE sim_account_new (
                    id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '默认模拟账户',
                    initial_capital REAL DEFAULT 100000.0,
                    current_capital REAL DEFAULT 100000.0,
                    cash_balance REAL DEFAULT 100000.0,
                    total_equity REAL DEFAULT 100000.0,
                    total_return_pct REAL DEFAULT 0.0,
                    total_trades INTEGER DEFAULT 0,
                    win_rate REAL DEFAULT 0.0,
                    peak_equity REAL DEFAULT 0,
                    account_ref TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock',
                    UNIQUE(user_id, market, account_ref)
                )
            """)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO sim_account_new
                (id, name, initial_capital, current_capital, cash_balance, total_equity,
                 total_return_pct, total_trades, win_rate, peak_equity, account_ref,
                 created_at, updated_at, user_id, market)
                SELECT id, name, initial_capital, current_capital, cash_balance, total_equity,
                       total_return_pct, total_trades, win_rate, {peak_expr}, {account_ref_expr},
                       created_at, updated_at, COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM sim_account
                """
            )
            conn.execute("DROP TABLE sim_account")
            conn.execute("ALTER TABLE sim_account_new RENAME TO sim_account")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sim_account_user ON sim_account(user_id)")
            logger.info("sim_account 已重建为 (user_id, market, account_ref) 多账户槽")
        except sqlite3.OperationalError as e:
            logger.warning("sim_account 多账户迁移失败（可忽略）: %s", e)


    def _migrate_vip_imports_account_ref(self, conn) -> None:
        try:
            cols = self._table_cols(conn, "vip_imports")
            if not cols:
                return
            ddl = self._table_sql(conn, "vip_imports")
            if "account_ref" in cols and "UNIQUE(user_id, market, account_ref, file_hash)" in ddl:
                return
            account_ref_expr = "COALESCE(account_ref, '')" if "account_ref" in cols else "''"
            conn.execute("""
                CREATE TABLE vip_imports_new (
                    id TEXT PRIMARY KEY,
                    file_name TEXT DEFAULT '',
                    file_hash TEXT DEFAULT '',
                    file_type TEXT DEFAULT '',
                    detected_kind TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'imported'
                        CHECK(status IN ('imported','duplicate','rejected','unparseable')),
                    summary TEXT DEFAULT '',
                    key_metrics_json TEXT DEFAULT '{}',
                    reason TEXT DEFAULT '',
                    account_ref TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock',
                    UNIQUE(user_id, market, account_ref, file_hash)
                )
            """)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO vip_imports_new
                (id, file_name, file_hash, file_type, detected_kind, status, summary,
                 key_metrics_json, reason, account_ref, created_at, user_id, market)
                SELECT id, file_name, file_hash, file_type, detected_kind, status, summary,
                       key_metrics_json, reason, {account_ref_expr}, created_at,
                       COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM vip_imports
                """
            )
            conn.execute("DROP TABLE vip_imports")
            conn.execute("ALTER TABLE vip_imports_new RENAME TO vip_imports")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vip_imports_user ON vip_imports(user_id, market, account_ref, created_at DESC)")
            logger.info("vip_imports 已重建为按账户隔离")
        except sqlite3.OperationalError as e:
            logger.warning("vip_imports 多账户迁移失败（可忽略）: %s", e)


    def _migrate_vip_derivative_terms_account_ref(self, conn) -> None:
        try:
            cols = self._table_cols(conn, "vip_derivative_terms")
            if not cols:
                return
            ddl = self._table_sql(conn, "vip_derivative_terms")
            # 目标态：既按账户隔离，又含 lot_key（同标的多笔头寸判别）。两者齐备才跳过。
            if "account_ref" in cols and "lot_key" in cols and "underlying_symbol, lot_key)" in ddl:
                return
            account_ref_expr = "COALESCE(account_ref, '')" if "account_ref" in cols else "''"
            lot_key_expr = "COALESCE(lot_key, '')" if "lot_key" in cols else "''"
            # is_indicative 是后加列；此重建须带上它，否则古老库(pre-account_ref)重建后丢列 → 后续
            # save_derivative_term 引用 is_indicative 会 OperationalError。
            indic_expr = "COALESCE(is_indicative, 0)" if "is_indicative" in cols else "0"
            conn.execute("""
                CREATE TABLE vip_derivative_terms_new (
                    id TEXT PRIMARY KEY,
                    source_file_name TEXT DEFAULT '',
                    source_file_hash TEXT DEFAULT '',
                    broker TEXT DEFAULT '',
                    product_family TEXT DEFAULT '',
                    underlying_symbol TEXT DEFAULT '',
                    currency TEXT DEFAULT 'USD',
                    terms_json TEXT DEFAULT '{}',
                    rationale_ref TEXT DEFAULT '',
                    account_ref TEXT DEFAULT '',
                    lot_key TEXT DEFAULT '',
                    is_indicative INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock',
                    UNIQUE(user_id, market, account_ref, source_file_hash, product_family, underlying_symbol, lot_key)
                )
            """)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO vip_derivative_terms_new
                (id, source_file_name, source_file_hash, broker, product_family, underlying_symbol,
                 currency, terms_json, rationale_ref, account_ref, lot_key, is_indicative, created_at, user_id, market)
                SELECT id, source_file_name, source_file_hash, broker, product_family, underlying_symbol,
                       currency, terms_json, rationale_ref, {account_ref_expr}, {lot_key_expr}, {indic_expr}, created_at,
                       COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM vip_derivative_terms
                """
            )
            conn.execute("DROP TABLE vip_derivative_terms")
            conn.execute("ALTER TABLE vip_derivative_terms_new RENAME TO vip_derivative_terms")
            logger.info("vip_derivative_terms 已重建为按账户隔离")
        except sqlite3.OperationalError as e:
            logger.warning("vip_derivative_terms 多账户迁移失败（可忽略）: %s", e)


    def _migrate_flag_indicative_derivative_terms(self, conn) -> None:
        """历史脏行一次性标记：把「产品介绍/推介稿」(indicative term sheet，非成交持仓)行 is_indicative=1。

        新上传已在 classify_pdf 处以 product_intro 拦截不入库；本迁移只清历史入库的脏行。
        # ponytail: 判据是最佳努力启发式——原 PDF 文本未持久化(库里只有 terms_json)，无法复用
        #   _is_indicative_intro 的 "Indicative Terms" 原文判别，只能用行内可得信号。宁漏勿误杀：
        #   ①文件名含 indicative(推介稿命名惯例，主信号)；②既无 MTM/名义、又无 lot_key
        #   (真月结单必有 MTM；真成交单/条款单——irf/termsheet/docx——均有 lot_key 非空)。
        #   不用 trade_date：推介稿也常带成交日("28 July 2026")，加该条件会漏标(实测泄漏行皆带 td)。
        #   标记可逆(错标 UPDATE is_indicative=0)、不 DELETE；精确根治需重传原 PDF 走新 classify 门。
        """
        try:
            if "is_indicative" not in self._table_cols(conn, "vip_derivative_terms"):
                return
            conn.execute(
                """
                UPDATE vip_derivative_terms SET is_indicative = 1
                 WHERE COALESCE(is_indicative, 0) = 0
                   AND (
                        LOWER(source_file_name) LIKE '%indicative%'
                     OR (
                            json_extract(terms_json, '$.market_value_usd') IS NULL
                        AND json_extract(terms_json, '$.notional')         IS NULL
                        AND COALESCE(lot_key, '') = ''
                     )
                   )
                """
            )
        except sqlite3.OperationalError as e:
            logger.warning("vip_derivative_terms 推介稿标记迁移失败（可忽略）: %s", e)


    def _migrate_vip_projections_lot_key(self, conn) -> None:
        """给 vip_projections 加 lot_key 列并把 UNIQUE 纳入 lot_key（同标的多笔逐日推算不折叠）。"""
        try:
            cols = self._table_cols(conn, "vip_projections")
            if not cols:
                return
            ddl = self._table_sql(conn, "vip_projections")
            if "lot_key" in cols and "kind, ticker, lot_key)" in ddl:
                return
            lot_key_expr = "COALESCE(lot_key, '')" if "lot_key" in cols else "''"
            conn.execute("""
                CREATE TABLE vip_projections_new (
                    id                TEXT PRIMARY KEY,
                    account_ref       TEXT DEFAULT '',
                    as_of_date        TEXT NOT NULL,
                    kind              TEXT NOT NULL DEFAULT 'stock_mtm'
                                      CHECK(kind IN ('stock_mtm','deriv_accum','deriv_settle')),
                    ticker            TEXT DEFAULT '',
                    lot_key           TEXT DEFAULT '',
                    quantity          REAL DEFAULT 0,
                    market_value_base REAL DEFAULT 0,
                    unrealized_pnl    REAL DEFAULT 0,
                    basis_json        TEXT DEFAULT '{}',
                    status            TEXT NOT NULL DEFAULT 'pending'
                                      CHECK(status IN ('pending','calibrated','flagged')),
                    confidence        REAL DEFAULT 0.5 CHECK(confidence BETWEEN 0 AND 1),
                    calibrated_by_doc_id TEXT DEFAULT '',
                    calib_diff_pct    REAL,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT DEFAULT '',
                    user_id           TEXT DEFAULT '',
                    market            TEXT DEFAULT 'us_stock',
                    UNIQUE(user_id, market, account_ref, as_of_date, kind, ticker, lot_key)
                )
            """)
            conn.execute(
                f"""
                INSERT OR IGNORE INTO vip_projections_new
                (id, account_ref, as_of_date, kind, ticker, lot_key, quantity, market_value_base,
                 unrealized_pnl, basis_json, status, confidence, calibrated_by_doc_id, calib_diff_pct,
                 created_at, updated_at, user_id, market)
                SELECT id, account_ref, as_of_date, kind, ticker, {lot_key_expr}, quantity, market_value_base,
                       unrealized_pnl, basis_json, status, confidence, calibrated_by_doc_id, calib_diff_pct,
                       created_at, COALESCE(updated_at,''), COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM vip_projections
                """
            )
            conn.execute("DROP TABLE vip_projections")
            conn.execute("ALTER TABLE vip_projections_new RENAME TO vip_projections")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vip_proj_acct "
                         "ON vip_projections(user_id, market, account_ref, as_of_date DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vip_proj_status "
                         "ON vip_projections(user_id, market, status)")
            logger.info("vip_projections 已重建为含 lot_key")
        except sqlite3.OperationalError as e:
            logger.warning("vip_projections lot_key 迁移失败（可忽略）: %s", e)


    def _migrate_vip_reports_account_ref(self, conn) -> None:
        try:
            cols = self._table_cols(conn, "vip_reports")
            if not cols or "account_ref" in cols:
                return
            conn.execute("""
                CREATE TABLE vip_reports_new (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'periodic'
                         CHECK(kind IN ('periodic','alert','import_snapshot')),
                    period TEXT DEFAULT '',
                    report_md TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    alert_key TEXT DEFAULT '',
                    account_ref TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock'
                )
            """)
            conn.execute("""
                INSERT INTO vip_reports_new
                (id, kind, period, report_md, payload_json, alert_key, account_ref, created_at, user_id, market)
                SELECT id, kind, period, report_md, payload_json, alert_key, '', created_at,
                       COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM vip_reports
            """)
            conn.execute("DROP TABLE vip_reports")
            conn.execute("ALTER TABLE vip_reports_new RENAME TO vip_reports")
            logger.info("vip_reports 已补 account_ref")
        except sqlite3.OperationalError as e:
            logger.warning("vip_reports 多账户迁移失败（可忽略）: %s", e)


    def _migrate_chat_sessions_account_ref(self, conn) -> None:
        try:
            cols = self._table_cols(conn, "chat_sessions")
            if not cols or "account_ref" in cols:
                return
            conn.execute("""
                CREATE TABLE chat_sessions_new (
                    id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    summarized_upto TEXT DEFAULT '',
                    msg_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active' CHECK(status IN ('active','archived')),
                    account_ref TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock'
                )
            """)
            conn.execute("""
                INSERT INTO chat_sessions_new
                (id, title, summary, summarized_upto, msg_count, status, account_ref,
                 created_at, updated_at, user_id, market)
                SELECT id, title, summary, summarized_upto, msg_count, status, '',
                       created_at, updated_at, COALESCE(user_id,''), COALESCE(market,'us_stock')
                  FROM chat_sessions
            """)
            conn.execute("DROP TABLE chat_sessions")
            conn.execute("ALTER TABLE chat_sessions_new RENAME TO chat_sessions")
            logger.info("chat_sessions 已补 account_ref")
        except sqlite3.OperationalError as e:
            logger.warning("chat_sessions 多账户迁移失败（可忽略）: %s", e)


    def _migrate_chat_messages_account_ref(self, conn) -> None:
        try:
            cols = self._table_cols(conn, "chat_messages")
            if not cols or "account_ref" in cols:
                return
            conn.execute("""
                CREATE TABLE chat_messages_new (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    content TEXT DEFAULT '',
                    tool_calls TEXT DEFAULT '[]',
                    tool_name TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    in_tokens INTEGER DEFAULT 0,
                    out_tokens INTEGER DEFAULT 0,
                    fail_reason TEXT DEFAULT '',
                    account_ref TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    user_id TEXT DEFAULT '',
                    market TEXT DEFAULT 'us_stock',
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                INSERT INTO chat_messages_new
                (id, session_id, role, content, tool_calls, tool_name, provider, model,
                 in_tokens, out_tokens, fail_reason, account_ref, created_at, user_id, market)
                SELECT m.id, m.session_id, m.role, m.content, m.tool_calls, m.tool_name, m.provider, m.model,
                       m.in_tokens, m.out_tokens, m.fail_reason, COALESCE(s.account_ref, ''),
                       m.created_at, COALESCE(m.user_id,''), COALESCE(m.market,'us_stock')
                  FROM chat_messages m
             LEFT JOIN chat_sessions s ON s.id = m.session_id
            """)
            conn.execute("DROP TABLE chat_messages")
            conn.execute("ALTER TABLE chat_messages_new RENAME TO chat_messages")
            logger.info("chat_messages 已补 account_ref")
        except sqlite3.OperationalError as e:
            logger.warning("chat_messages 多账户迁移失败（可忽略）: %s", e)


    def _migrate_experience_cards_widen_scope(self, conn) -> None:
        """放宽 experience_cards.scope 的 CHECK，纳入 VIP 复盘卡片作用域（vip_portfolio/macro/ticker）。

        旧基表 CHECK(scope IN ('global','sector','ticker')) 会拒绝 VIP 顾问策略复盘卡片入库。
        列级 CHECK 无法 ALTER，只能重建表。幂等：DDL 已含 'vip_portfolio' 则跳过。卡片可再生，重建低风险。
        按当前列的交集搬迁，兼容尚未跑全 ALTER 的旧库。
        """
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='experience_cards'").fetchone()
            if not row or not row["sql"] or "vip_portfolio" in row["sql"]:
                return  # 表不存在或已放宽
            conn.execute("""
                CREATE TABLE experience_cards_new (
                    id TEXT PRIMARY KEY,
                    scope TEXT DEFAULT 'global'
                        CHECK(scope IN ('global','sector','ticker','vip_portfolio','vip_macro','vip_ticker')),
                    scope_key TEXT DEFAULT '',
                    category TEXT DEFAULT 'lesson' CHECK(category IN ('pattern','lesson','rule')),
                    title TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT DEFAULT '[]',
                    confidence REAL DEFAULT 0.5, applied_count INTEGER DEFAULT 0,
                    source_review_id TEXT, created_at TEXT NOT NULL, updated_at TEXT,
                    user_id TEXT DEFAULT '', win_count INTEGER DEFAULT 0, loss_count INTEGER DEFAULT 0,
                    last_applied_at TEXT, market TEXT DEFAULT 'us_stock'
                )
            """)
            known = {"id", "scope", "scope_key", "category", "title", "content", "evidence",
                     "confidence", "applied_count", "source_review_id", "created_at", "updated_at",
                     "user_id", "win_count", "loss_count", "last_applied_at", "market"}
            common = [c for c in (self._table_cols(conn, "experience_cards") or []) if c in known]
            collist = ", ".join(common)
            conn.execute(f"INSERT INTO experience_cards_new({collist}) SELECT {collist} FROM experience_cards")
            conn.execute("DROP TABLE experience_cards")
            conn.execute("ALTER TABLE experience_cards_new RENAME TO experience_cards")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_scope ON experience_cards(scope, scope_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_confidence ON experience_cards(confidence DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_experience_market ON experience_cards(market)")
            logger.info("experience_cards 已重建：scope CHECK 纳入 vip_portfolio/macro/ticker")
        except sqlite3.OperationalError as e:
            logger.warning("experience_cards 放宽 scope 重建失败（可忽略）: %s", e)


    def _migrate_experience_cards_fts(self, conn) -> None:
        """P0-③：给 experience_cards 建 FTS5 外部内容全文索引 + 同步触发器 + 存量回填（幂等）。

        先建虚表：无 fts5 的 sqlite 构建会在此抛 OperationalError → 记 debug 后【整体跳过】，
        绝不继续建触发器（否则触发器经延迟解析仍建成，之后 INSERT 卡片写不存在的 _fts 表致插入崩）。
        回填只在虚表【本次首建】(建前不在 sqlite_master)且基表有存量时用 fts5 'rebuild' 重建一次
        （存量卡片先于触发器存在的情形）；之后触发器保持同步，不再回填。
        （注：外部内容表 count(*) 读的是内容表行数、非已索引数，故不能用 count 判空，改用「建前是否已存在」。）
        ponytail: 依赖本方法在 _migrate_experience_cards_widen_scope 之后调用——后者可能重建基表，
        须先重建完再挂 fts 触发器，避免悬空。widen_scope 是终态幂等（纳入 vip_portfolio 后不再重建），故无二次悬空风险。
        """
        try:
            existed = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='experience_cards_fts'").fetchone()
            conn.execute(_EC_FTS_TABLE)
        except sqlite3.OperationalError as e:
            logger.debug("experience_cards FTS5 不可用（sqlite 无 fts5 模块），全文检索降级为 scope 粗筛: %s", e)
            return
        try:
            for trig in _EC_FTS_TRIGGERS:
                conn.execute(trig)
            if not existed:  # 虚表首建：把先于触发器存在的存量卡片一次性灌入索引
                base_n = conn.execute("SELECT count(*) FROM experience_cards").fetchone()[0]
                if base_n:
                    conn.execute("INSERT INTO experience_cards_fts(experience_cards_fts) VALUES('rebuild')")
                    logger.info("experience_cards_fts 存量回填 %d 张卡片（fts5 rebuild）", base_n)
        except sqlite3.OperationalError as e:
            logger.warning("experience_cards FTS5 触发器/回填失败（可忽略，检索降级 scope 粗筛）: %s", e)


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

