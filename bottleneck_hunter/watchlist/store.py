"""SQLite persistence for the watchlist tracking system.

Follows the same patterns as ``dataflows/store.py``:
raw SQL, WAL mode, ``sqlite3.Row`` factory, ``_MIGRATIONS`` list.
Separate DB file ``data/watchlist.db`` to avoid migration conflicts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "watchlist.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS watchlist (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL UNIQUE,
    company_name    TEXT NOT NULL,
    company_name_cn TEXT DEFAULT '',
    market          TEXT DEFAULT 'us_stock',
    tier            TEXT NOT NULL CHECK(tier IN ('focus','normal','track')),
    tier_rank       INTEGER DEFAULT 0,
    composite_score REAL DEFAULT 0.0,
    source          TEXT DEFAULT 'manual',
    source_analysis_id TEXT,
    sector          TEXT DEFAULT '',
    bottleneck_node TEXT DEFAULT '',
    added_at        TEXT NOT NULL,
    updated_at      TEXT,
    notes           TEXT DEFAULT '',
    is_active       INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    ticker       TEXT NOT NULL,
    date         TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       INTEGER,
    market_cap   REAL,
    pe_ratio     REAL,
    change_pct   REAL,
    rsi_14       REAL,
    macd         REAL,
    macd_signal  REAL,
    macd_hist    REAL,
    sma_20       REAL,
    sma_50       REAL,
    fetched_at   TEXT,
    market       TEXT DEFAULT 'us_stock',
    UNIQUE(ticker, date)
);

CREATE TABLE IF NOT EXISTS news_digest (
    id            TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT DEFAULT '',
    sentiment     TEXT DEFAULT '',
    sentiment_score REAL DEFAULT 0.0,
    source_url    TEXT DEFAULT '',
    source_name   TEXT DEFAULT '',
    llm_analysis  TEXT DEFAULT '',
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS sec_filings (
    id            TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    filing_type   TEXT NOT NULL,
    filed_date    TEXT NOT NULL,
    title         TEXT DEFAULT '',
    summary       TEXT DEFAULT '',
    url           TEXT DEFAULT '',
    is_insider_trade INTEGER DEFAULT 0,
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS insider_trades (
    id            TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    insider_name  TEXT NOT NULL,
    insider_title TEXT DEFAULT '',
    transaction_type TEXT DEFAULT '',
    shares        INTEGER DEFAULT 0,
    price         REAL,
    total_value   REAL,
    date          TEXT NOT NULL,
    source_filing_id TEXT DEFAULT '',
    fetched_at    TEXT
);

CREATE TABLE IF NOT EXISTS options_activity (
    id               TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    date             TEXT NOT NULL,
    unusual_volume   INTEGER DEFAULT 0,
    put_call_ratio   REAL,
    total_call_volume INTEGER DEFAULT 0,
    total_put_volume  INTEGER DEFAULT 0,
    max_oi_strike    REAL,
    max_oi_expiry    TEXT DEFAULT '',
    notable_trades   TEXT DEFAULT '[]',
    fetched_at       TEXT
);

CREATE TABLE IF NOT EXISTS earnings_reports (
    id               TEXT PRIMARY KEY,
    ticker           TEXT NOT NULL,
    report_date      TEXT NOT NULL,
    fiscal_quarter   TEXT DEFAULT '',
    eps_actual       REAL,
    eps_estimate     REAL,
    eps_surprise_pct REAL,
    revenue_actual   REAL,
    revenue_estimate REAL,
    guidance         TEXT DEFAULT '',
    fetched_at       TEXT
);

CREATE TABLE IF NOT EXISTS llm_budget (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT NOT NULL,
    provider         TEXT DEFAULT '',
    model            TEXT DEFAULT '',
    input_tokens     INTEGER DEFAULT 0,
    output_tokens    INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    task_type        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pipeline_status (
    pipeline_name TEXT PRIMARY KEY,
    last_run_at   TEXT,
    last_status   TEXT DEFAULT 'idle',
    last_error    TEXT DEFAULT '',
    next_run_at   TEXT,
    stocks_processed INTEGER DEFAULT 0,
    stocks_total  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uzi_analyses (
    id            TEXT PRIMARY KEY,
    entry_id      TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    analysis_type TEXT NOT NULL,
    status        TEXT DEFAULT 'running',
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    result_json   TEXT,
    summary       TEXT DEFAULT '',
    score         REAL,
    signal        TEXT,
    trap_level    TEXT,
    FOREIGN KEY (entry_id) REFERENCES watchlist(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stock_intelligence (
    id              TEXT PRIMARY KEY,
    entry_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    price_summary   TEXT DEFAULT '{}',
    news_summary    TEXT DEFAULT '{}',
    sec_summary     TEXT DEFAULT '{}',
    options_summary TEXT DEFAULT '{}',
    earnings_summary TEXT DEFAULT '{}',
    source_scorecard_summary TEXT DEFAULT '{}',
    brief_text      TEXT DEFAULT '',
    key_signals     TEXT DEFAULT '[]',
    data_freshness  TEXT DEFAULT '{}',
    status          TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    error           TEXT DEFAULT '',
    FOREIGN KEY (entry_id) REFERENCES watchlist(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS strategy_records (
    id              TEXT PRIMARY KEY,
    entry_id        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    intelligence_id TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    intelligence_summary TEXT DEFAULT '',
    bull_bear_analysis   TEXT DEFAULT '{}',
    core_logic           TEXT DEFAULT '',
    action_strategy      TEXT DEFAULT '{}',
    risk_control         TEXT DEFAULT '{}',
    targets_timeline     TEXT DEFAULT '{}',
    strategy_comparison  TEXT DEFAULT '{}',
    confidence_rating    TEXT DEFAULT '{}',
    signal          TEXT DEFAULT 'neutral' CHECK(signal IN ('bullish','neutral','bearish')),
    confidence      INTEGER DEFAULT 5 CHECK(confidence BETWEEN 1 AND 10),
    reasoning_chain TEXT DEFAULT '',
    status          TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    error           TEXT DEFAULT '',
    FOREIGN KEY (entry_id) REFERENCES watchlist(id) ON DELETE CASCADE,
    FOREIGN KEY (intelligence_id) REFERENCES stock_intelligence(id)
);

CREATE TABLE IF NOT EXISTS macro_strategies (
    id                  TEXT PRIMARY KEY,
    version             INTEGER NOT NULL DEFAULT 1,
    regime              TEXT DEFAULT 'sideways',
    risk_appetite       TEXT DEFAULT 'balanced',
    recommended_cash_pct REAL DEFAULT 25.0,
    market_summary      TEXT DEFAULT '',
    key_signals         TEXT DEFAULT '[]',
    sector_rotation     TEXT DEFAULT '{}',
    risk_factors        TEXT DEFAULT '[]',
    strategy_text       TEXT DEFAULT '',
    valid_until_trigger TEXT DEFAULT '',
    result_json         TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'valid' CHECK(status IN ('valid','needs_minor_tweak','needs_major_revision','superseded')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT,
    expires_at          TEXT
);

CREATE TABLE IF NOT EXISTS strategic_plans (
    id                  TEXT PRIMARY KEY,
    macro_strategy_id   TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    overall_stance      TEXT DEFAULT 'balanced',
    target_allocation   TEXT DEFAULT '{}',
    sector_targets      TEXT DEFAULT '{}',
    stock_selection     TEXT DEFAULT '{}',
    risk_limits         TEXT DEFAULT '{}',
    rebalancing_triggers TEXT DEFAULT '[]',
    strategy_text       TEXT DEFAULT '',
    result_json         TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'valid' CHECK(status IN ('valid','superseded','invalidated')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS tactical_plans (
    id                  TEXT PRIMARY KEY,
    strategic_plan_id   TEXT,
    entry_id            TEXT,
    ticker              TEXT NOT NULL,
    plan_date           TEXT NOT NULL,
    action              TEXT DEFAULT 'hold',
    entry_plan          TEXT DEFAULT '{}',
    exit_plan           TEXT DEFAULT '{}',
    catalyst_watch      TEXT DEFAULT '[]',
    confidence          INTEGER DEFAULT 5,
    result_json         TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'active' CHECK(status IN ('active','executed','expired','cancelled')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS execution_plans (
    id                  TEXT PRIMARY KEY,
    tactical_plan_id    TEXT,
    entry_id            TEXT,
    ticker              TEXT NOT NULL,
    action              TEXT NOT NULL,
    shares              INTEGER DEFAULT 0,
    target_price        REAL,
    amount              REAL DEFAULT 0,
    method              TEXT DEFAULT 'market',
    priority            INTEGER DEFAULT 5,
    confidence          INTEGER DEFAULT 5,
    reasoning           TEXT DEFAULT '',
    result_json         TEXT DEFAULT '{}',
    status              TEXT DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected','executed','expired')),
    rejection_reason    TEXT DEFAULT '',
    created_at          TEXT NOT NULL,
    confirmed_at        TEXT,
    executed_at         TEXT
);

CREATE TABLE IF NOT EXISTS committee_reviews (
    id                  TEXT PRIMARY KEY,
    execution_plan_id   TEXT NOT NULL,
    member_role         TEXT NOT NULL,
    model_provider      TEXT DEFAULT '',
    model_name          TEXT DEFAULT '',
    vote                TEXT DEFAULT 'approve',
    confidence          INTEGER DEFAULT 5,
    score               REAL,
    key_concerns        TEXT DEFAULT '[]',
    suggestions         TEXT DEFAULT '[]',
    result_json         TEXT DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS committee_consensus (
    id                  TEXT PRIMARY KEY,
    execution_plan_id   TEXT NOT NULL,
    final_verdict       TEXT NOT NULL,
    approval_rate       REAL DEFAULT 0.0,
    vote_detail         TEXT DEFAULT '{}',
    consensus_modifications TEXT DEFAULT '[]',
    final_execution_plan TEXT DEFAULT '[]',
    key_risks_flagged   TEXT DEFAULT '[]',
    minority_opinions   TEXT DEFAULT '[]',
    summary             TEXT DEFAULT '',
    result_json         TEXT DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalyst_tracking (
    id                  TEXT PRIMARY KEY,
    entry_id            TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    catalyst_type       TEXT DEFAULT 'event',
    title               TEXT NOT NULL,
    description         TEXT DEFAULT '',
    expected_date       TEXT,
    actual_date         TEXT,
    impact_level        TEXT DEFAULT 'medium' CHECK(impact_level IN ('low','medium','high','critical')),
    confidence          INTEGER DEFAULT 5,
    status              TEXT DEFAULT 'pending' CHECK(status IN ('pending','monitoring','triggered','expired','cancelled')),
    outcome             TEXT DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS trade_feedback (
    id                  TEXT PRIMARY KEY,
    execution_plan_id   TEXT,
    ticker              TEXT NOT NULL,
    feedback_type       TEXT DEFAULT 'rejection',
    reason              TEXT DEFAULT '',
    user_note           TEXT DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_reviews (
    id                  TEXT PRIMARY KEY,
    sim_trade_id        TEXT,
    ticker              TEXT NOT NULL,
    review_type         TEXT DEFAULT 'trade_close',
    entry_price         REAL,
    exit_price          REAL,
    return_pct          REAL,
    lessons_learned     TEXT DEFAULT '',
    experience_card     TEXT DEFAULT '{}',
    result_json         TEXT DEFAULT '{}',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_account (
    id                  TEXT PRIMARY KEY,
    name                TEXT DEFAULT '默认模拟账户',
    initial_capital     REAL DEFAULT 100000.0,
    current_capital     REAL DEFAULT 100000.0,
    cash_balance        REAL DEFAULT 100000.0,
    total_equity        REAL DEFAULT 100000.0,
    total_return_pct    REAL DEFAULT 0.0,
    total_trades        INTEGER DEFAULT 0,
    win_rate            REAL DEFAULT 0.0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS sim_positions (
    id                  TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    entry_id            TEXT,
    ticker              TEXT NOT NULL,
    shares              INTEGER DEFAULT 0,
    avg_cost            REAL DEFAULT 0.0,
    current_price       REAL DEFAULT 0.0,
    market_value        REAL DEFAULT 0.0,
    unrealized_pnl      REAL DEFAULT 0.0,
    weight_pct          REAL DEFAULT 0.0,
    opened_at           TEXT NOT NULL,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS sim_trades (
    id                  TEXT PRIMARY KEY,
    account_id          TEXT NOT NULL,
    execution_plan_id   TEXT,
    entry_id            TEXT,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL CHECK(side IN ('buy','sell')),
    shares              INTEGER NOT NULL,
    price               REAL NOT NULL,
    amount              REAL NOT NULL,
    commission          REAL DEFAULT 0.0,
    trade_type          TEXT DEFAULT 'entry',
    reasoning           TEXT DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_preferences (
    id                  TEXT PRIMARY KEY,
    key                 TEXT NOT NULL UNIQUE,
    value               TEXT NOT NULL,
    category            TEXT DEFAULT 'general',
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS institutional_holders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    holder_name   TEXT NOT NULL,
    shares        INTEGER DEFAULT 0,
    value         REAL DEFAULT 0.0,
    pct_held      REAL DEFAULT 0.0,
    date          TEXT DEFAULT '',
    fetched_at    TEXT,
    UNIQUE(ticker, holder_name, date)
);

CREATE TABLE IF NOT EXISTS analyst_ratings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    firm          TEXT NOT NULL,
    rating        TEXT DEFAULT '',
    target_price  REAL,
    date          TEXT DEFAULT '',
    fetched_at    TEXT,
    UNIQUE(ticker, firm, date)
);

CREATE TABLE IF NOT EXISTS macro_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    indicator   TEXT NOT NULL,
    value       REAL,
    market      TEXT DEFAULT 'global',
    fetched_at  TEXT,
    UNIQUE(date, indicator)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id              TEXT PRIMARY KEY,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    initial_capital REAL DEFAULT 100000.0,
    final_equity    REAL DEFAULT 0.0,
    total_return_pct REAL DEFAULT 0.0,
    sharpe_ratio    REAL DEFAULT 0.0,
    sortino_ratio   REAL DEFAULT 0.0,
    max_drawdown_pct REAL DEFAULT 0.0,
    calmar_ratio    REAL DEFAULT 0.0,
    win_rate_pct    REAL DEFAULT 0.0,
    trade_count     INTEGER DEFAULT 0,
    equity_curve    TEXT DEFAULT '[]',
    created_at      TEXT NOT NULL
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker_date ON market_snapshots(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_news_ticker_date ON news_digest(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON sec_filings(ticker, filed_date DESC);
CREATE INDEX IF NOT EXISTS idx_insider_ticker ON insider_trades(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_options_ticker ON options_activity(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_earnings_ticker ON earnings_reports(ticker, report_date DESC);
CREATE INDEX IF NOT EXISTS idx_budget_date ON llm_budget(date);
CREATE INDEX IF NOT EXISTS idx_watchlist_tier ON watchlist(tier, composite_score DESC);
CREATE INDEX IF NOT EXISTS idx_uzi_entry ON uzi_analyses(entry_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_uzi_ticker ON uzi_analyses(ticker, analysis_type);
CREATE INDEX IF NOT EXISTS idx_intelligence_entry ON stock_intelligence(entry_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intelligence_ticker ON stock_intelligence(ticker, version DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_entry ON strategy_records(entry_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_ticker ON strategy_records(ticker, version DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_signal ON strategy_records(signal, confidence DESC);
CREATE INDEX IF NOT EXISTS idx_macro_version ON macro_strategies(version DESC);
CREATE INDEX IF NOT EXISTS idx_macro_status ON macro_strategies(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategic_version ON strategic_plans(version DESC);
CREATE INDEX IF NOT EXISTS idx_strategic_macro ON strategic_plans(macro_strategy_id);
CREATE INDEX IF NOT EXISTS idx_tactical_date ON tactical_plans(plan_date DESC);
CREATE INDEX IF NOT EXISTS idx_tactical_ticker ON tactical_plans(ticker, plan_date DESC);
CREATE INDEX IF NOT EXISTS idx_tactical_strategic ON tactical_plans(strategic_plan_id);
CREATE INDEX IF NOT EXISTS idx_execution_status ON execution_plans(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_execution_ticker ON execution_plans(ticker, status);
CREATE INDEX IF NOT EXISTS idx_committee_execution ON committee_reviews(execution_plan_id);
CREATE INDEX IF NOT EXISTS idx_consensus_execution ON committee_consensus(execution_plan_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_entry ON catalyst_tracking(entry_id, status);
CREATE INDEX IF NOT EXISTS idx_catalyst_ticker ON catalyst_tracking(ticker, expected_date);
CREATE INDEX IF NOT EXISTS idx_feedback_ticker ON trade_feedback(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sim_trades_ticker ON sim_trades(ticker, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sim_positions_account ON sim_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_inst_holders_ticker ON institutional_holders(ticker, date DESC);
CREATE INDEX IF NOT EXISTS idx_analyst_ratings_ticker ON analyst_ratings(ticker, date DESC);
"""

_MIGRATIONS: list[str] = [
    # 8B.4: experience_cards table
    """CREATE TABLE IF NOT EXISTS experience_cards (
        id              TEXT PRIMARY KEY,
        scope           TEXT DEFAULT 'global' CHECK(scope IN ('global','sector','ticker')),
        scope_key       TEXT DEFAULT '',
        category        TEXT DEFAULT 'lesson' CHECK(category IN ('pattern','lesson','rule')),
        title           TEXT NOT NULL,
        content         TEXT NOT NULL,
        evidence        TEXT DEFAULT '[]',
        confidence      REAL DEFAULT 0.5,
        applied_count   INTEGER DEFAULT 0,
        source_review_id TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_exp_scope ON experience_cards(scope, scope_key)",
    "CREATE INDEX IF NOT EXISTS idx_exp_confidence ON experience_cards(confidence DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auto_reviews_ticker ON auto_reviews(ticker, created_at DESC)",
    # 9A: tuning_log table
    """CREATE TABLE IF NOT EXISTS tuning_log (
        id              TEXT PRIMARY KEY,
        type            TEXT DEFAULT 'weight' CHECK(type IN ('weight','threshold','prompt','rule')),
        parameter_name  TEXT NOT NULL,
        old_value       TEXT DEFAULT '',
        new_value       TEXT DEFAULT '',
        reason          TEXT DEFAULT '',
        evidence        TEXT DEFAULT '[]',
        status          TEXT DEFAULT 'proposed' CHECK(status IN ('proposed','approved','rejected')),
        proposed_at     TEXT NOT NULL,
        decided_at      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tuning_status ON tuning_log(status, proposed_at DESC)",
    # Phase 16B: 多用户 — 为所有表添加 user_id 列
    "ALTER TABLE watchlist ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE market_snapshots ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE news_digest ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE sec_filings ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE insider_trades ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE options_activity ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE earnings_reports ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE llm_budget ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE pipeline_status ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE budget_config ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE uzi_analyses ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE stock_intelligence ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE strategy_records ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE macro_strategies ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE strategic_plans ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE tactical_plans ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE execution_plans ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE committee_reviews ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE committee_consensus ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE catalyst_tracking ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE trade_feedback ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE auto_reviews ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE sim_account ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE sim_positions ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE sim_trades ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE user_preferences ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE experience_cards ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE tuning_log ADD COLUMN user_id TEXT DEFAULT ''",
    # 索引
    "CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_user ON market_snapshots(user_id)",
    "ALTER TABLE market_snapshots ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "CREATE INDEX IF NOT EXISTS idx_intelligence_user ON stock_intelligence(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_user ON strategy_records(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_macro_user ON macro_strategies(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sim_account_user ON sim_account(user_id)",
    # watchlist 唯一约束需要包含 user_id — 用新索引实现（旧 UNIQUE(ticker) 不能 ALTER）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_user_ticker ON watchlist(user_id, ticker)",
    # 17A.5: 机构持仓与分析师评级表 user_id
    "ALTER TABLE institutional_holders ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE analyst_ratings ADD COLUMN user_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_inst_holders_user ON institutional_holders(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_analyst_ratings_user ON analyst_ratings(user_id)",
    "ALTER TABLE backtest_runs ADD COLUMN user_id TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_backtest_user ON backtest_runs(user_id)",
    # 17D.4: 催化剂结果判定字段
    "ALTER TABLE catalyst_tracking ADD COLUMN outcome_impact REAL DEFAULT 0",
    "ALTER TABLE catalyst_tracking ADD COLUMN judged_at TEXT",
    # 17F.4: A/B 对比快照表
    """CREATE TABLE IF NOT EXISTS ab_snapshots (
        id          TEXT PRIMARY KEY,
        label       TEXT NOT NULL,
        params_json TEXT DEFAULT '{}',
        user_id     TEXT DEFAULT '',
        created_at  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ab_snapshots_user ON ab_snapshots(user_id, created_at DESC)",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Store class
# ---------------------------------------------------------------------------

_DB_LOCKS: dict[str, threading.Lock] = {}
_DB_LOCKS_GUARD = threading.Lock()


def _get_db_lock(db_path: str) -> threading.Lock:
    with _DB_LOCKS_GUARD:
        if db_path not in _DB_LOCKS:
            _DB_LOCKS[db_path] = threading.Lock()
        return _DB_LOCKS[db_path]


class WatchlistStore:
    def __init__(self, db_path: str | Path | None = None, user_id: str = ""):
        self._db_path = str(db_path or _DEFAULT_DB)
        self._user_id = user_id
        self._write_lock = _get_db_lock(self._db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def for_user(self, user_id: str) -> "WatchlistStore":
        """返回绑定指定用户的 store 克隆（共享同一 DB 和写锁）。"""
        clone = object.__new__(WatchlistStore)
        clone._db_path = self._db_path
        clone._user_id = user_id
        clone._write_lock = self._write_lock
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
        query = query[:insert_pos] + clause + query[insert_pos:]
        return query, params + (self._user_id,)

    def _user_insert_cols(self) -> str:
        """返回 INSERT 语句中的 user_id 列名。"""
        return ", user_id" if self._user_id else ""

    def _user_insert_vals(self) -> str:
        """返回 INSERT 语句中的 user_id 占位符。"""
        return ", ?" if self._user_id else ""

    def _user_insert_params(self) -> tuple:
        """返回 INSERT 语句中的 user_id 参数。"""
        return (self._user_id,) if self._user_id else ()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    from contextlib import contextmanager

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
                except sqlite3.OperationalError:
                    pass
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

    # ------------------------------------------------------------------
    # Watchlist CRUD
    # ------------------------------------------------------------------

    _TIER_LIMITS = {"focus": 6, "normal": 6, "track": 12}

    def add(self, entry: dict) -> str:
        """Add a stock to the watchlist. Returns entry id. Raises ValueError on capacity overflow."""
        conn = self._connect()
        try:
            tier = entry.get("tier", "track")
            # 检查容量
            q, p = self._user_filter("SELECT COUNT(*) AS cnt FROM watchlist WHERE tier = ?", (tier,))
            row = conn.execute(q, p).fetchone()
            if row["cnt"] >= self._TIER_LIMITS.get(tier, 12):
                raise ValueError(f"Tier '{tier}' is full (max {self._TIER_LIMITS[tier]})")
            q, p = self._user_filter("SELECT COUNT(*) AS cnt FROM watchlist")
            total = conn.execute(q, p).fetchone()
            wl_limit = getattr(self, '_watchlist_limit', 24)
            if total["cnt"] >= wl_limit:
                raise ValueError(f"Watchlist is full (max {wl_limit})")
            # 检查重复
            q, p = self._user_filter("SELECT id FROM watchlist WHERE ticker = ?", (entry["ticker"],))
            existing = conn.execute(q, p).fetchone()
            if existing:
                raise ValueError(f"Ticker '{entry['ticker']}' already in watchlist")

            entry_id = entry.get("id") or uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                f"""INSERT INTO watchlist
                   (id, ticker, company_name, company_name_cn, market, tier, tier_rank,
                    composite_score, source, source_analysis_id, sector, bottleneck_node,
                    added_at, updated_at, notes, is_active{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    entry_id,
                    entry["ticker"],
                    entry.get("company_name", entry["ticker"]),
                    entry.get("company_name_cn", ""),
                    entry.get("market", "us_stock"),
                    tier,
                    entry.get("tier_rank", 0),
                    entry.get("composite_score", 0.0),
                    entry.get("source", "manual"),
                    entry.get("source_analysis_id"),
                    entry.get("sector", ""),
                    entry.get("bottleneck_node", ""),
                    now,
                    now,
                    entry.get("notes", ""),
                    1,
                ) + self._user_insert_params(),
            )
            conn.commit()
            return entry_id
        finally:
            conn.close()

    def remove(self, entry_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._user_filter("DELETE FROM watchlist WHERE id = ?", (entry_id,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update(self, entry_id: str, **fields) -> bool:
        if not fields:
            return False
        allowed = {"tier", "tier_rank", "composite_score", "notes", "is_active", "updated_at",
                   "source_analysis_id", "bottleneck_node", "sector", "company_name_cn"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        if "updated_at" not in fields:
            parts.append("updated_at = ?")
            vals.append(_now_iso())
        vals.append(entry_id)
        conn = self._connect()
        try:
            q, p = self._user_filter(f"UPDATE watchlist SET {', '.join(parts)} WHERE id = ?", tuple(vals))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM watchlist WHERE id = ?", (entry_id,))
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_by_ticker(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM watchlist WHERE ticker = ?", (ticker,))
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_all(self, tier: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if tier:
                q, p = self._user_filter(
                    "SELECT * FROM watchlist WHERE tier = ? ORDER BY composite_score DESC, tier_rank ASC",
                    (tier,),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM watchlist ORDER BY tier, composite_score DESC, tier_rank ASC"
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_by_tier(self) -> dict[str, int]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT tier, COUNT(*) AS cnt FROM watchlist GROUP BY tier")
            rows = conn.execute(q, p).fetchall()
            result = {"focus": 0, "normal": 0, "track": 0}
            for r in rows:
                result[r["tier"]] = r["cnt"]
            return result
        finally:
            conn.close()

    def get_tickers(self) -> list[str]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT ticker FROM watchlist WHERE is_active = 1")
            rows = conn.execute(q, p).fetchall()
            return [r["ticker"] for r in rows]
        finally:
            conn.close()

    def get_tickers_by_market(self) -> dict[str, list[str]]:
        """按市场分组返回活跃 ticker。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT ticker, market FROM watchlist WHERE is_active = 1"
            )
            rows = conn.execute(q, p).fetchall()
            result: dict[str, list[str]] = {}
            for r in rows:
                result.setdefault(r["market"] or "us_stock", []).append(r["ticker"])
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Market Snapshots
    # ------------------------------------------------------------------

    def save_snapshots(self, snapshots: list[dict]) -> int:
        if not snapshots:
            return 0
        with self._write_conn() as conn:
            count = 0
            for s in snapshots:
                conn.execute(
                    f"""INSERT OR REPLACE INTO market_snapshots
                       (ticker, date, open, high, low, close, volume, market_cap,
                        pe_ratio, change_pct, rsi_14, macd, macd_signal, macd_hist,
                        sma_20, sma_50, fetched_at, market{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        s["ticker"], s["date"], s.get("open"), s.get("high"),
                        s.get("low"), s.get("close"), s.get("volume"),
                        s.get("market_cap"), s.get("pe_ratio"), s.get("change_pct"),
                        s.get("rsi_14"), s.get("macd"), s.get("macd_signal"),
                        s.get("macd_hist"), s.get("sma_20"), s.get("sma_50"),
                        s.get("fetched_at", _now_iso()),
                        s.get("market", "us_stock"),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count

    def get_snapshots(self, ticker: str, days: int = 90) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, days),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_snapshot(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def save_news(self, items: list[dict]) -> int:
        if not items:
            return 0
        conn = self._connect()
        try:
            count = 0
            for n in items:
                nid = n.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO news_digest
                       (id, ticker, date, title, summary, sentiment, sentiment_score,
                        source_url, source_name, llm_analysis, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        nid, n["ticker"], n["date"], n["title"],
                        n.get("summary", ""), n.get("sentiment", ""),
                        n.get("sentiment_score", 0.0), n.get("source_url", ""),
                        n.get("source_name", ""), n.get("llm_analysis", ""),
                        n.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_news(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM news_digest WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # SEC Filings
    # ------------------------------------------------------------------

    def save_filings(self, filings: list[dict]) -> int:
        if not filings:
            return 0
        conn = self._connect()
        try:
            count = 0
            for f in filings:
                fid = f.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO sec_filings
                       (id, ticker, filing_type, filed_date, title, summary, url,
                        is_insider_trade, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        fid, f["ticker"], f["filing_type"], f["filed_date"],
                        f.get("title", ""), f.get("summary", ""), f.get("url", ""),
                        1 if f.get("is_insider_trade") else 0,
                        f.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_filings(self, ticker: str, filing_type: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if filing_type:
                q, p = self._user_filter(
                    "SELECT * FROM sec_filings WHERE ticker = ? AND filing_type = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, filing_type, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM sec_filings WHERE ticker = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Insider Trades
    # ------------------------------------------------------------------

    def save_insider_trades(self, trades: list[dict]) -> int:
        if not trades:
            return 0
        conn = self._connect()
        try:
            count = 0
            for t in trades:
                tid = t.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO insider_trades
                       (id, ticker, insider_name, insider_title, transaction_type,
                        shares, price, total_value, date, source_filing_id, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        tid, t["ticker"], t["insider_name"], t.get("insider_title", ""),
                        t.get("transaction_type", ""), t.get("shares", 0),
                        t.get("price"), t.get("total_value"), t["date"],
                        t.get("source_filing_id", ""), t.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_insider_trades(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM insider_trades WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Options Activity
    # ------------------------------------------------------------------

    def save_options(self, activities: list[dict]) -> int:
        if not activities:
            return 0
        conn = self._connect()
        try:
            count = 0
            for a in activities:
                aid = a.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO options_activity
                       (id, ticker, date, unusual_volume, put_call_ratio,
                        total_call_volume, total_put_volume, max_oi_strike,
                        max_oi_expiry, notable_trades, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        aid, a["ticker"], a["date"],
                        1 if a.get("unusual_volume") else 0,
                        a.get("put_call_ratio"), a.get("total_call_volume", 0),
                        a.get("total_put_volume", 0), a.get("max_oi_strike"),
                        a.get("max_oi_expiry", ""),
                        json.dumps(a.get("notable_trades", []), ensure_ascii=False),
                        a.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_options(self, ticker: str, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM options_activity WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if isinstance(d.get("notable_trades"), str):
                    try:
                        d["notable_trades"] = json.loads(d["notable_trades"])
                    except (json.JSONDecodeError, TypeError):
                        d["notable_trades"] = []
                result.append(d)
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Earnings Reports
    # ------------------------------------------------------------------

    def save_earnings(self, reports: list[dict]) -> int:
        if not reports:
            return 0
        conn = self._connect()
        try:
            count = 0
            for e in reports:
                eid = e.get("id") or uuid.uuid4().hex[:12]
                conn.execute(
                    f"""INSERT OR IGNORE INTO earnings_reports
                       (id, ticker, report_date, fiscal_quarter, eps_actual,
                        eps_estimate, eps_surprise_pct, revenue_actual,
                        revenue_estimate, guidance, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        eid, e["ticker"], e["report_date"],
                        e.get("fiscal_quarter", ""),
                        e.get("eps_actual"), e.get("eps_estimate"),
                        e.get("eps_surprise_pct"), e.get("revenue_actual"),
                        e.get("revenue_estimate"), e.get("guidance", ""),
                        e.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_earnings(self, ticker: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM earnings_reports WHERE ticker = ? ORDER BY report_date DESC",
                (ticker,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # LLM Budget
    # ------------------------------------------------------------------

    def record_llm_usage(self, usage: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO llm_budget
                   (date, provider, model, input_tokens, output_tokens, estimated_cost_usd, task_type{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    usage.get("date", _today()),
                    usage.get("provider", ""), usage.get("model", ""),
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    usage.get("estimated_cost_usd", 0.0), usage.get("task_type", ""),
                ) + self._user_insert_params(),
            )
            conn.commit()
        finally:
            conn.close()

    def get_daily_usage(self, date: str | None = None) -> dict:
        date = date or _today()
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date = ?""",
                (date,),
            )
            row = conn.execute(q, p).fetchone()
            return {"date": date, "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"], "cost": row["cost"]}
        finally:
            conn.close()

    def get_monthly_usage(self, year: int | None = None, month: int | None = None) -> dict:
        now = datetime.now(timezone.utc)
        year = year or now.year
        month = month or now.month
        prefix = f"{year}-{month:02d}"
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date LIKE ?""",
                (f"{prefix}%",),
            )
            row = conn.execute(q, p).fetchone()
            return {"year": year, "month": month, "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"], "cost": row["cost"]}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Budget config
    # ------------------------------------------------------------------

    def get_budget_limits(self) -> dict[str, float]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT key, value FROM budget_config")
            rows = conn.execute(q, p).fetchall()
            return {r["key"]: float(r["value"]) for r in rows}
        finally:
            conn.close()

    def set_budget_limit(self, key: str, value: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO budget_config(key, value{self._user_insert_cols()}) VALUES (?, ?{self._user_insert_vals()})",
                (key, str(value)) + self._user_insert_params(),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Pipeline Status
    # ------------------------------------------------------------------

    def update_pipeline_status(self, name: str, **fields) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR IGNORE INTO pipeline_status(pipeline_name{self._user_insert_cols()}) VALUES (?{self._user_insert_vals()})",
                (name,) + self._user_insert_params(),
            )
            allowed = {"last_run_at", "last_status", "last_error", "next_run_at",
                        "stocks_processed", "stocks_total"}
            parts, vals = [], []
            for k, v in fields.items():
                if k in allowed:
                    parts.append(f"{k} = ?")
                    vals.append(v)
            if parts:
                vals.append(name)
                q, p = self._user_filter(
                    f"UPDATE pipeline_status SET {', '.join(parts)} WHERE pipeline_name = ?", tuple(vals)
                )
                conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def get_pipeline_statuses(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM pipeline_status")
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stale_tickers(self, max_age_hours: int = 48) -> list[dict]:
        """返回快照数据超过 max_age_hours 的活跃 ticker 列表。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """
                SELECT w.ticker, w.company_name, w.market,
                       MAX(ms.date) AS last_date
                FROM watchlist w
                LEFT JOIN market_snapshots ms ON w.ticker = ms.ticker
                WHERE w.is_active = 1
                GROUP BY w.ticker
                HAVING last_date IS NULL
                   OR last_date < date('now', ?)
                """,
                (f"-{max_age_hours} hours",),
                table="w",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Macro Snapshots
    # ------------------------------------------------------------------

    def save_macro_snapshot(self, indicator: str, date: str, value: float,
                           fetched_at: str | None = None) -> None:
        with self._write_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO macro_snapshots
                   (date, indicator, value, fetched_at)
                   VALUES (?, ?, ?, ?)""",
                (date, indicator, value, fetched_at or _now_iso()),
            )

    def get_latest_macro_snapshots(self) -> list[dict]:
        """返回每个指标最新一条记录。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT indicator, value, date, fetched_at
                   FROM macro_snapshots
                   WHERE (indicator, date) IN (
                       SELECT indicator, MAX(date) FROM macro_snapshots GROUP BY indicator
                   )"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_macro_history(self, indicator: str, days: int = 30) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM macro_snapshots WHERE indicator = ? ORDER BY date DESC LIMIT ?",
                (indicator, days),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # UZI analyses
    # ------------------------------------------------------------------

    def create_uzi_analysis(self, entry_id: str, ticker: str,
                            analysis_type: str) -> str:
        analysis_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO uzi_analyses
                   (id, entry_id, ticker, analysis_type, status, started_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (analysis_id, entry_id, ticker, analysis_type, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return analysis_id
        finally:
            conn.close()

    def complete_uzi_analysis(self, analysis_id: str, result_json: str,
                               summary: str = "", score: float | None = None,
                               signal: str | None = None,
                               trap_level: str | None = None) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE uzi_analyses SET
                   status='completed', completed_at=?, result_json=?,
                   summary=?, score=?, signal=?, trap_level=?
                   WHERE id=?""",
                (_now_iso(), result_json, summary, score, signal,
                 trap_level, analysis_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def fail_uzi_analysis(self, analysis_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE uzi_analyses SET
                   status='failed', completed_at=?, summary=?
                   WHERE id=?""",
                (_now_iso(), error, analysis_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def get_uzi_history(self, entry_id: str,
                        limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, analysis_type, status,
                   started_at, completed_at, summary, score, signal, trap_level
                   FROM uzi_analyses WHERE entry_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_uzi_analysis(self, analysis_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM uzi_analyses WHERE id = ?", (analysis_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Intelligence records
    # ------------------------------------------------------------------

    def create_intelligence(self, entry_id: str, ticker: str) -> tuple[str, int]:
        """Create a new intelligence record. Returns (intelligence_id, version)."""
        intel_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM stock_intelligence WHERE entry_id = ?",
                (entry_id,)
            )
            version = conn.execute(q, p).fetchone()[0]
            conn.execute(
                f"""INSERT INTO stock_intelligence
                   (id, entry_id, ticker, version, status, created_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (intel_id, entry_id, ticker, version, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return intel_id, version
        finally:
            conn.close()

    def complete_intelligence(self, intel_id: str, *,
                              price_summary: str = "{}",
                              news_summary: str = "{}",
                              sec_summary: str = "{}",
                              options_summary: str = "{}",
                              earnings_summary: str = "{}",
                              source_scorecard_summary: str = "{}",
                              brief_text: str = "",
                              key_signals: str = "[]",
                              data_freshness: str = "{}") -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE stock_intelligence SET
                   status='completed', completed_at=?,
                   price_summary=?, news_summary=?, sec_summary=?,
                   options_summary=?, earnings_summary=?, source_scorecard_summary=?,
                   brief_text=?, key_signals=?, data_freshness=?
                   WHERE id=?""",
                (_now_iso(), price_summary, news_summary, sec_summary,
                 options_summary, earnings_summary, source_scorecard_summary,
                 brief_text, key_signals, data_freshness, intel_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def fail_intelligence(self, intel_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE stock_intelligence SET
                   status='failed', completed_at=?, error=?
                   WHERE id=?""",
                (_now_iso(), error, intel_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def get_latest_intelligence(self, entry_id: str) -> dict | None:
        """Returns the most recent completed intelligence record."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT * FROM stock_intelligence
                   WHERE entry_id = ? AND status = 'completed'
                   ORDER BY version DESC LIMIT 1""",
                (entry_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_intelligence_history(self, entry_id: str, limit: int = 10) -> list[dict]:
        """Returns intelligence history (metadata only, no full JSON bodies)."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, version, status,
                   created_at, completed_at, error, data_freshness
                   FROM stock_intelligence WHERE entry_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_intelligence(self, intel_id: str) -> dict | None:
        """Returns a single intelligence record with all JSON fields."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM stock_intelligence WHERE id = ?", (intel_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Strategy records
    # ------------------------------------------------------------------

    def create_strategy(self, entry_id: str, ticker: str,
                        intelligence_id: str | None = None) -> tuple[str, int]:
        """Create a new strategy record. Returns (strategy_id, version)."""
        strategy_id = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_records WHERE entry_id = ?",
                (entry_id,)
            )
            version = conn.execute(q, p).fetchone()[0]
            conn.execute(
                f"""INSERT INTO strategy_records
                   (id, entry_id, ticker, intelligence_id, version, status, created_at{self._user_insert_cols()})
                   VALUES (?, ?, ?, ?, ?, 'running', ?{self._user_insert_vals()})""",
                (strategy_id, entry_id, ticker, intelligence_id, version, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return strategy_id, version
        finally:
            conn.close()

    def complete_strategy(self, strategy_id: str, *,
                          intelligence_summary: str = "",
                          bull_bear_analysis: str = "{}",
                          core_logic: str = "",
                          action_strategy: str = "{}",
                          risk_control: str = "{}",
                          targets_timeline: str = "{}",
                          strategy_comparison: str = "{}",
                          confidence_rating: str = "{}",
                          signal: str = "neutral",
                          confidence: int = 5,
                          reasoning_chain: str = "") -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE strategy_records SET
                   status='completed', completed_at=?,
                   intelligence_summary=?, bull_bear_analysis=?, core_logic=?,
                   action_strategy=?, risk_control=?, targets_timeline=?,
                   strategy_comparison=?, confidence_rating=?,
                   signal=?, confidence=?, reasoning_chain=?
                   WHERE id=?""",
                (_now_iso(), intelligence_summary, bull_bear_analysis, core_logic,
                 action_strategy, risk_control, targets_timeline,
                 strategy_comparison, confidence_rating,
                 signal, confidence, reasoning_chain, strategy_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def fail_strategy(self, strategy_id: str, error: str) -> None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """UPDATE strategy_records SET
                   status='failed', completed_at=?, error=?
                   WHERE id=?""",
                (_now_iso(), error, strategy_id),
            )
            conn.execute(q, p)
            conn.commit()
        finally:
            conn.close()

    def get_latest_strategy(self, entry_id: str) -> dict | None:
        """Returns the most recent completed strategy record."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT * FROM strategy_records
                   WHERE entry_id = ? AND status = 'completed'
                   ORDER BY version DESC LIMIT 1""",
                (entry_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_strategy_history(self, entry_id: str, limit: int = 10) -> list[dict]:
        """Returns strategy history (metadata only)."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, entry_id, ticker, intelligence_id, version,
                   signal, confidence, status, created_at, completed_at, error
                   FROM strategy_records WHERE entry_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (entry_id, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_strategy(self, strategy_id: str) -> dict | None:
        """Returns a single strategy record with all fields."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM strategy_records WHERE id = ?", (strategy_id,)
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_previous_strategy(self, entry_id: str,
                              exclude_id: str | None = None) -> dict | None:
        """Returns the most recent completed strategy BEFORE the current one."""
        conn = self._connect()
        try:
            if exclude_id:
                q, p = self._user_filter(
                    """SELECT * FROM strategy_records
                       WHERE entry_id = ? AND status = 'completed' AND id != ?
                       ORDER BY version DESC LIMIT 1""",
                    (entry_id, exclude_id)
                )
                row = conn.execute(q, p).fetchone()
            else:
                # Get second-most-recent
                q, p = self._user_filter(
                    """SELECT * FROM strategy_records
                       WHERE entry_id = ? AND status = 'completed'
                       ORDER BY version DESC LIMIT 1 OFFSET 1""",
                    (entry_id,)
                )
                row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_all_strategy_summaries(self) -> dict[str, dict]:
        """Returns {entry_id: {signal, confidence, version, created_at}} for all entries."""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT entry_id, signal, confidence, version, created_at
                   FROM strategy_records
                   WHERE status = 'completed'
                   AND (entry_id, version) IN (
                       SELECT entry_id, MAX(version)
                       FROM strategy_records
                       WHERE status = 'completed'
                       GROUP BY entry_id
                   )"""
            )
            rows = conn.execute(q, p).fetchall()
            return {
                r["entry_id"]: {
                    "signal": r["signal"],
                    "confidence": r["confidence"],
                    "version": r["version"],
                    "created_at": r["created_at"],
                }
                for r in rows
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Macro Strategies (L1)
    # ------------------------------------------------------------------

    def create_macro_strategy(self, result_json: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM macro_strategies"
            )
            version = conn.execute(q, p).fetchone()[0]
            q, p = self._user_filter(
                "UPDATE macro_strategies SET status = 'superseded' WHERE status = 'valid'"
            )
            conn.execute(q, p)
            now = _now_iso()
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO macro_strategies
                   (id, version, regime, risk_appetite, recommended_cash_pct,
                    market_summary, key_signals, sector_rotation, risk_factors,
                    strategy_text, valid_until_trigger, result_json, status, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    sid, version,
                    rj.get("regime", "sideways"),
                    rj.get("risk_appetite", "balanced"),
                    rj.get("recommended_cash_pct", 25.0),
                    rj.get("market_summary", ""),
                    json.dumps(rj.get("key_signals", []), ensure_ascii=False),
                    json.dumps(rj.get("sector_rotation", {}), ensure_ascii=False),
                    json.dumps(rj.get("risk_factors", []), ensure_ascii=False),
                    rj.get("strategy_text", ""),
                    rj.get("valid_until_trigger", ""),
                    json.dumps(rj, ensure_ascii=False),
                    "valid", now, now,
                ) + self._user_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()

    def get_latest_macro_strategy(self) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM macro_strategies WHERE status = 'valid' ORDER BY version DESC LIMIT 1"
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                q, p = self._user_filter(
                    "SELECT * FROM macro_strategies ORDER BY version DESC LIMIT 1"
                )
                row = conn.execute(q, p).fetchone()
            return self._parse_macro_row(row) if row else None
        finally:
            conn.close()

    def get_macro_history(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, version, regime, risk_appetite, market_summary,
                   status, created_at, updated_at
                   FROM macro_strategies ORDER BY version DESC LIMIT ?""",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_macro_status(self, strategy_id: str, status: str,
                            minor_tweaks: list | None = None) -> bool:
        conn = self._connect()
        try:
            parts = ["status = ?", "updated_at = ?"]
            vals = [status, _now_iso()]
            if minor_tweaks is not None:
                q, p = self._user_filter(
                    "SELECT result_json FROM macro_strategies WHERE id = ?", (strategy_id,)
                )
                row = conn.execute(q, p).fetchone()
                if row:
                    rj = json.loads(row["result_json"] or "{}")
                    rj["minor_tweaks"] = minor_tweaks
                    parts.append("result_json = ?")
                    vals.append(json.dumps(rj, ensure_ascii=False))
            vals.append(strategy_id)
            q, p = self._user_filter(
                f"UPDATE macro_strategies SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def _parse_macro_row(self, row) -> dict:
        d = dict(row)
        for field in ("key_signals", "risk_factors"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        for field in ("sector_rotation", "result_json"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = {}
        return d

    # ------------------------------------------------------------------
    # Strategic Plans (L2)
    # ------------------------------------------------------------------

    def create_strategic_plan(self, macro_strategy_id: str, result_json: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategic_plans"
            )
            version = conn.execute(q, p).fetchone()[0]
            q, p = self._user_filter(
                "UPDATE strategic_plans SET status = 'superseded' WHERE status = 'valid'"
            )
            conn.execute(q, p)
            now = _now_iso()
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO strategic_plans
                   (id, macro_strategy_id, version, overall_stance, target_allocation,
                    sector_targets, stock_selection, risk_limits, rebalancing_triggers,
                    strategy_text, result_json, status, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    sid, macro_strategy_id, version,
                    rj.get("overall_stance", "balanced"),
                    json.dumps(rj.get("target_allocation", {}), ensure_ascii=False),
                    json.dumps(rj.get("sector_targets", {}), ensure_ascii=False),
                    json.dumps(rj.get("stock_selection", {}), ensure_ascii=False),
                    json.dumps(rj.get("risk_limits", {}), ensure_ascii=False),
                    json.dumps(rj.get("rebalancing_triggers", []), ensure_ascii=False),
                    rj.get("strategy_text", ""),
                    json.dumps(rj, ensure_ascii=False),
                    "valid", now, now,
                ) + self._user_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()

    def get_latest_strategic_plan(self) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM strategic_plans WHERE status = 'valid' ORDER BY version DESC LIMIT 1"
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                q, p = self._user_filter(
                    "SELECT * FROM strategic_plans ORDER BY version DESC LIMIT 1"
                )
                row = conn.execute(q, p).fetchone()
            return self._parse_strategic_row(row) if row else None
        finally:
            conn.close()

    def get_strategic_history(self, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT id, macro_strategy_id, version, overall_stance,
                   status, created_at, updated_at
                   FROM strategic_plans ORDER BY version DESC LIMIT ?""",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _parse_strategic_row(self, row) -> dict:
        d = dict(row)
        for field in ("target_allocation", "sector_targets", "stock_selection",
                      "risk_limits", "result_json"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = {}
        if isinstance(d.get("rebalancing_triggers"), str):
            try:
                d["rebalancing_triggers"] = json.loads(d["rebalancing_triggers"])
            except (json.JSONDecodeError, TypeError):
                d["rebalancing_triggers"] = []
        return d

    # ------------------------------------------------------------------
    # Tactical Plans (L3)
    # ------------------------------------------------------------------

    def create_tactical_plan(self, strategic_plan_id: str, entry_id: str,
                             ticker: str, plan_date: str, result_json: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO tactical_plans
                   (id, strategic_plan_id, entry_id, ticker, plan_date, action,
                    entry_plan, exit_plan, catalyst_watch, confidence,
                    result_json, status, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    sid, strategic_plan_id, entry_id, ticker, plan_date,
                    rj.get("action", "hold"),
                    json.dumps(rj.get("entry_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("exit_plan", {}), ensure_ascii=False),
                    json.dumps(rj.get("catalyst_watch", []), ensure_ascii=False),
                    rj.get("confidence", 5),
                    json.dumps(rj, ensure_ascii=False),
                    "active", _now_iso(), _now_iso(),
                ) + self._user_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()

    def get_tactical_plans_by_date(self, plan_date: str | None = None) -> list[dict]:
        plan_date = plan_date or _today()
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM tactical_plans WHERE plan_date = ? ORDER BY confidence DESC",
                (plan_date,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("entry_plan", "exit_plan", "result_json"),
                                            ("catalyst_watch",)) for r in rows]
        finally:
            conn.close()

    def get_tactical_plan_for_ticker(self, ticker: str, plan_date: str | None = None) -> dict | None:
        plan_date = plan_date or _today()
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM tactical_plans WHERE ticker = ? AND plan_date = ? AND status = 'active' LIMIT 1",
                (ticker, plan_date),
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            return self._parse_json_fields(dict(row), ("entry_plan", "exit_plan", "result_json"),
                                           ("catalyst_watch",))
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Execution Plans (L4)
    # ------------------------------------------------------------------

    def create_execution_plan(self, tactical_plan_id: str, entry_id: str,
                              ticker: str, result_json: dict) -> str:
        sid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO execution_plans
                   (id, tactical_plan_id, entry_id, ticker, action, shares,
                    target_price, amount, method, priority, confidence,
                    reasoning, result_json, status, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    sid, tactical_plan_id, entry_id, ticker,
                    rj.get("action", "hold"),
                    rj.get("shares", 0),
                    rj.get("target_price") or rj.get("estimated_price"),
                    rj.get("amount", 0) or rj.get("estimated_amount", 0),
                    rj.get("method") or rj.get("execution_method", "market"),
                    rj.get("priority", 5) if isinstance(rj.get("priority"), int) else 5,
                    rj.get("confidence", 5),
                    rj.get("reasoning") or rj.get("rationale", ""),
                    json.dumps(rj, ensure_ascii=False),
                    "pending", _now_iso(),
                ) + self._user_insert_params(),
            )
            conn.commit()
            return sid
        finally:
            conn.close()

    def get_pending_executions(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM execution_plans WHERE status = 'pending' ORDER BY priority ASC, created_at ASC"
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",)) for r in rows]
        finally:
            conn.close()

    def confirm_execution(self, plan_id: str) -> bool:
        with self._write_conn() as conn:
            q, p = self._user_filter(
                "UPDATE execution_plans SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'",
                (_now_iso(), plan_id),
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def reject_execution(self, plan_id: str, reason: str = "") -> bool:
        with self._write_conn() as conn:
            q, p = self._user_filter(
                "UPDATE execution_plans SET status = 'rejected', rejection_reason = ? WHERE id = ? AND status = 'pending'",
                (reason, plan_id),
            )
            cur = conn.execute(q, p)
            if cur.rowcount > 0:
                q2, p2 = self._user_filter("SELECT ticker FROM execution_plans WHERE id = ?", (plan_id,))
                row = conn.execute(q2, p2).fetchone()
                if row:
                    conn.execute(
                        f"""INSERT INTO trade_feedback
                           (id, execution_plan_id, ticker, feedback_type, reason, created_at{self._user_insert_cols()})
                           VALUES (?,?,?,?,?,?{self._user_insert_vals()})""",
                        (uuid.uuid4().hex[:12], plan_id, row["ticker"], "rejection",
                         reason, _now_iso()) + self._user_insert_params(),
                    )
            return cur.rowcount > 0

    def get_execution_plan(self, plan_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM execution_plans WHERE id = ?", (plan_id,))
            row = conn.execute(q, p).fetchone()
            return self._parse_json_fields(dict(row), ("result_json",)) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Committee Reviews
    # ------------------------------------------------------------------

    def create_committee_review(self, execution_plan_id: str, member_role: str,
                                model_provider: str, model_name: str,
                                result_json: dict) -> str:
        rid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO committee_reviews
                   (id, execution_plan_id, member_role, model_provider, model_name,
                    vote, confidence, score, key_concerns, suggestions,
                    result_json, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    rid, execution_plan_id, member_role, model_provider, model_name,
                    rj.get("vote", "approve"),
                    rj.get("confidence", 5),
                    rj.get("score") or rj.get("risk_score") or rj.get("growth_score")
                    or rj.get("value_score") or rj.get("contrarian_score"),
                    json.dumps(rj.get("key_concerns", []), ensure_ascii=False),
                    json.dumps(rj.get("suggestions", []), ensure_ascii=False),
                    json.dumps(rj, ensure_ascii=False),
                    _now_iso(),
                ) + self._user_insert_params(),
            )
            conn.commit()
            return rid
        finally:
            conn.close()

    def get_reviews_for_execution(self, execution_plan_id: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM committee_reviews WHERE execution_plan_id = ? ORDER BY created_at",
                (execution_plan_id,),
            )
            rows = conn.execute(q, p).fetchall()
            return [self._parse_json_fields(dict(r), ("result_json",),
                                            ("key_concerns", "suggestions")) for r in rows]
        finally:
            conn.close()

    def create_committee_consensus(self, execution_plan_id: str, result_json: dict) -> str:
        cid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            rj = result_json or {}
            conn.execute(
                f"""INSERT INTO committee_consensus
                   (id, execution_plan_id, final_verdict, approval_rate,
                    vote_detail, consensus_modifications, final_execution_plan,
                    key_risks_flagged, minority_opinions, summary, result_json, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (
                    cid, execution_plan_id,
                    rj.get("final_verdict", "approved"),
                    rj.get("approval_rate", 0.0),
                    json.dumps(rj.get("vote_detail", {}), ensure_ascii=False),
                    json.dumps(rj.get("consensus_modifications", []), ensure_ascii=False),
                    json.dumps(rj.get("final_execution_plan", []), ensure_ascii=False),
                    json.dumps(rj.get("key_risks_flagged", []), ensure_ascii=False),
                    json.dumps(rj.get("minority_opinions", []), ensure_ascii=False),
                    rj.get("summary", ""),
                    json.dumps(rj, ensure_ascii=False),
                    _now_iso(),
                ) + self._user_insert_params(),
            )
            conn.commit()
            return cid
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Catalyst Tracking
    # ------------------------------------------------------------------

    def create_catalyst(self, entry_id: str, ticker: str, title: str,
                        catalyst_type: str = "event", description: str = "",
                        expected_date: str | None = None,
                        impact_level: str = "medium", confidence: int = 5) -> str:
        cid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO catalyst_tracking
                   (id, entry_id, ticker, catalyst_type, title, description,
                    expected_date, impact_level, confidence, status, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (cid, entry_id, ticker, catalyst_type, title, description,
                 expected_date, impact_level, confidence, "pending", _now_iso(), _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return cid
        finally:
            conn.close()

    def get_catalysts_for_entry(self, entry_id: str, active_only: bool = True) -> list[dict]:
        conn = self._connect()
        try:
            if active_only:
                q, p = self._user_filter(
                    "SELECT * FROM catalyst_tracking WHERE entry_id = ? AND status IN ('pending','monitoring') ORDER BY expected_date",
                    (entry_id,),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM catalyst_tracking WHERE entry_id = ? ORDER BY created_at DESC",
                    (entry_id,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def update_catalyst_status(self, catalyst_id: str, status: str,
                               outcome: str = "", actual_date: str | None = None) -> bool:
        conn = self._connect()
        try:
            parts = ["status = ?", "updated_at = ?"]
            vals: list = [status, _now_iso()]
            if outcome:
                parts.append("outcome = ?")
                vals.append(outcome)
            if actual_date:
                parts.append("actual_date = ?")
                vals.append(actual_date)
            vals.append(catalyst_id)
            q, p = self._user_filter(
                f"UPDATE catalyst_tracking SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_catalysts_for_ticker(self, ticker: str) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM catalyst_tracking WHERE ticker = ? ORDER BY created_at DESC",
                (ticker,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_upcoming_catalysts(self, days: int = 14) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status IN ('pending','monitoring')
                   AND ct.expected_date IS NOT NULL
                   ORDER BY ct.expected_date ASC""",
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def expire_past_catalysts(self) -> int:
        conn = self._connect()
        try:
            today = _today()
            q, p = self._user_filter(
                """UPDATE catalyst_tracking SET status = 'expired', updated_at = ?
                   WHERE expected_date < ? AND status IN ('pending', 'monitoring')""",
                (_now_iso(), today),
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def get_expiring_catalysts(self, days: int = 7) -> list[dict]:
        conn = self._connect()
        try:
            today = _today()
            future = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
            q, p = self._user_filter(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status IN ('pending', 'monitoring')
                   AND ct.expected_date IS NOT NULL
                   AND ct.expected_date >= ? AND ct.expected_date <= ?
                   ORDER BY ct.expected_date ASC""",
                (today, future),
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_unjudged_expired_catalysts(self) -> list[dict]:
        """获取已过期但未判定结果的催化剂"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT ct.*, w.company_name FROM catalyst_tracking ct
                   LEFT JOIN watchlist w ON ct.entry_id = w.id
                   WHERE ct.status = 'expired'
                   AND (ct.outcome IS NULL OR ct.outcome = '')
                   ORDER BY ct.expected_date DESC""",
                table="ct",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def judge_catalyst(self, catalyst_id: str, outcome: str, impact: float,
                       actual_date: str | None = None) -> bool:
        """判定催化剂结果：realized / failed / partial"""
        conn = self._connect()
        try:
            parts = ["outcome = ?", "outcome_impact = ?", "judged_at = ?", "updated_at = ?"]
            now = _now_iso()
            vals: list = [outcome, impact, now, now]
            if actual_date:
                parts.append("actual_date = ?")
                vals.append(actual_date)
            # 如果 realized，同步更新 status 为 triggered
            if outcome == "realized":
                parts.append("status = ?")
                vals.append("triggered")
            vals.append(catalyst_id)
            q, p = self._user_filter(
                f"UPDATE catalyst_tracking SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Trade Feedback
    # ------------------------------------------------------------------

    def create_trade_feedback(self, execution_plan_id: str, ticker: str,
                              feedback_type: str = "rejection", reason: str = "",
                              user_note: str = "") -> str:
        fid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO trade_feedback
                   (id, execution_plan_id, ticker, feedback_type, reason, user_note, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (fid, execution_plan_id, ticker, feedback_type, reason, user_note, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return fid
        finally:
            conn.close()

    def get_rejection_patterns(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._user_filter(
                    "SELECT * FROM trade_feedback WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM trade_feedback ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Sim Account
    # ------------------------------------------------------------------

    def get_sim_account(self) -> dict:
        conn = self._connect()
        try:
            q, p = self._user_filter("SELECT * FROM sim_account LIMIT 1")
            row = conn.execute(q, p).fetchone()
            if row:
                return dict(row)
            aid = uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                f"""INSERT INTO sim_account
                   (id, name, initial_capital, current_capital, cash_balance,
                    total_equity, total_return_pct, total_trades, win_rate, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (aid, "默认模拟账户", 100000.0, 100000.0, 100000.0,
                 100000.0, 0.0, 0, 0.0, now, now) + self._user_insert_params(),
            )
            conn.commit()
            return {"id": aid, "name": "默认模拟账户", "initial_capital": 100000.0,
                    "current_capital": 100000.0, "cash_balance": 100000.0,
                    "total_equity": 100000.0, "total_return_pct": 0.0,
                    "total_trades": 0, "win_rate": 0.0, "created_at": now, "updated_at": now}
        finally:
            conn.close()

    def update_sim_account(self, **fields) -> bool:
        allowed = {"current_capital", "cash_balance", "total_equity", "total_return_pct",
                   "total_trades", "win_rate", "name", "initial_capital"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        parts.append("updated_at = ?")
        vals.append(_now_iso())
        account = self.get_sim_account()
        vals.append(account["id"])
        with self._write_conn() as conn:
            q, p = self._user_filter(
                f"UPDATE sim_account SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Sim Positions & Trades
    # ------------------------------------------------------------------

    def get_sim_positions(self, account_id: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if account_id:
                q, p = self._user_filter(
                    "SELECT * FROM sim_positions WHERE account_id = ? AND shares > 0", (account_id,)
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter("SELECT * FROM sim_positions WHERE shares > 0")
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def create_sim_trade(self, account_id: str, ticker: str, side: str,
                         shares: int, price: float, amount: float,
                         execution_plan_id: str | None = None,
                         entry_id: str | None = None,
                         trade_type: str = "entry", reasoning: str = "") -> str:
        tid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO sim_trades
                   (id, account_id, execution_plan_id, entry_id, ticker, side,
                    shares, price, amount, trade_type, reasoning, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (tid, account_id, execution_plan_id, entry_id, ticker, side,
                 shares, price, amount, trade_type, reasoning, _now_iso()) + self._user_insert_params(),
            )
            return tid

    def get_sim_position(self, account_id: str, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM sim_positions WHERE account_id = ? AND ticker = ? AND shares > 0",
                (account_id, ticker),
            )
            row = conn.execute(q, p).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_sim_position(self, account_id: str, ticker: str,
                            shares: int, avg_cost: float,
                            entry_id: str | None = None) -> str:
        pid = uuid.uuid4().hex[:12]
        now = _now_iso()
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO sim_positions
                   (id, account_id, entry_id, ticker, shares, avg_cost,
                    current_price, market_value, unrealized_pnl, weight_pct,
                    opened_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (pid, account_id, entry_id, ticker, shares, avg_cost,
                 avg_cost, shares * avg_cost, 0.0, 0.0, now, now) + self._user_insert_params(),
            )
            return pid

    def update_sim_position(self, position_id: str, **fields) -> bool:
        allowed = {"shares", "avg_cost", "current_price", "market_value",
                   "unrealized_pnl", "weight_pct", "entry_id"}
        parts, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                parts.append(f"{k} = ?")
                vals.append(v)
        if not parts:
            return False
        parts.append("updated_at = ?")
        vals.append(_now_iso())
        vals.append(position_id)
        with self._write_conn() as conn:
            q, p = self._user_filter(
                f"UPDATE sim_positions SET {', '.join(parts)} WHERE id = ?", tuple(vals)
            )
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def delete_sim_position(self, position_id: str) -> bool:
        with self._write_conn() as conn:
            q, p = self._user_filter("DELETE FROM sim_positions WHERE id = ?", (position_id,))
            cur = conn.execute(q, p)
            return cur.rowcount > 0

    def get_sim_trades(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._user_filter(
                    "SELECT * FROM sim_trades WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM sim_trades ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # User Preferences
    # ------------------------------------------------------------------

    def save_preference(self, key: str, value: str, category: str = "general") -> str:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT id FROM user_preferences WHERE key = ?", (key,)
            )
            existing = conn.execute(q, p).fetchone()
            if existing:
                q, p = self._user_filter(
                    "UPDATE user_preferences SET value = ?, category = ?, updated_at = ? WHERE key = ?",
                    (value, category, _now_iso(), key),
                )
                conn.execute(q, p)
                conn.commit()
                return existing["id"]
            pid = uuid.uuid4().hex[:12]
            conn.execute(
                f"INSERT INTO user_preferences (id, key, value, category, updated_at{self._user_insert_cols()}) VALUES (?,?,?,?,?{self._user_insert_vals()})",
                (pid, key, value, category, _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return pid
        finally:
            conn.close()

    def get_preferences(self, category: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if category:
                q, p = self._user_filter(
                    "SELECT * FROM user_preferences WHERE category = ?", (category,)
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter("SELECT * FROM user_preferences")
                rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_preference(self, key: str, default: str = "") -> str:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT value FROM user_preferences WHERE key = ?", (key,)
            )
            row = conn.execute(q, p).fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Auto Reviews (复盘)
    # ------------------------------------------------------------------

    def create_auto_review(self, sim_trade_id: str, ticker: str,
                           review_type: str = "trade_close",
                           entry_price: float = 0, exit_price: float = 0,
                           return_pct: float = 0, result_json: dict | None = None,
                           lessons_learned: str = "",
                           experience_card: dict | None = None) -> str:
        """写入复盘记录"""
        rid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO auto_reviews
                   (id, sim_trade_id, ticker, review_type, entry_price, exit_price,
                    return_pct, lessons_learned, experience_card, result_json, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (rid, sim_trade_id, ticker, review_type,
                 entry_price, exit_price, return_pct,
                 lessons_learned,
                 json.dumps(experience_card or {}, ensure_ascii=False),
                 json.dumps(result_json or {}, ensure_ascii=False),
                 _now_iso()) + self._user_insert_params(),
            )
        return rid

    def get_auto_reviews(self, ticker: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if ticker:
                q, p = self._user_filter(
                    "SELECT * FROM auto_reviews WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
                    (ticker, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM auto_reviews ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, dict_fields=("result_json", "experience_card"))
                result.append(d)
            return result
        finally:
            conn.close()

    def get_auto_review(self, review_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM auto_reviews WHERE id = ?", (review_id,)
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            self._parse_json_fields(d, dict_fields=("result_json", "experience_card"))
            return d
        finally:
            conn.close()

    def get_trades_without_review(self) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT st.* FROM sim_trades st
                   LEFT JOIN auto_reviews ar ON ar.sim_trade_id = st.id
                   WHERE st.side = 'sell' AND ar.id IS NULL
                   ORDER BY st.created_at DESC""",
                table="st",
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Experience Cards (经验卡片)
    # ------------------------------------------------------------------

    def create_experience_card(self, scope: str, scope_key: str,
                               category: str, title: str, content: str,
                               evidence: list | None = None,
                               confidence: float = 0.5,
                               source_review_id: str | None = None) -> str:
        """创建经验卡片"""
        cid = uuid.uuid4().hex[:12]
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT INTO experience_cards
                   (id, scope, scope_key, category, title, content, evidence,
                    confidence, source_review_id, created_at, updated_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (cid, scope, scope_key or "", category, title, content,
                 json.dumps(evidence or [], ensure_ascii=False),
                 confidence, source_review_id, _now_iso(), _now_iso()) + self._user_insert_params(),
            )
        return cid

    def get_experience_cards(self, scope: str | None = None,
                             scope_key: str | None = None,
                             limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            conditions = []
            params: list = []
            if scope:
                conditions.append("scope = ?")
                params.append(scope)
            if scope_key:
                conditions.append("scope_key = ?")
                params.append(scope_key)
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            params.append(limit)
            q, p = self._user_filter(
                f"SELECT * FROM experience_cards{where} ORDER BY confidence DESC, applied_count DESC LIMIT ?",
                tuple(params),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()

    def get_relevant_cards(self, ticker: str, sector: str = "",
                           limit: int = 5) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                """SELECT * FROM experience_cards
                   WHERE (scope = 'global')
                      OR (scope = 'ticker' AND scope_key = ?)
                      OR (scope = 'sector' AND scope_key = ?)
                   ORDER BY confidence DESC, applied_count DESC
                   LIMIT ?""",
                (ticker, sector, limit),
            )
            rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()

    def increment_card_applied(self, card_id: str) -> None:
        """经验卡片被引用时，递增 applied_count"""
        with self._write_conn() as conn:
            q, p = self._user_filter(
                "UPDATE experience_cards SET applied_count = applied_count + 1, updated_at = ? WHERE id = ?",
                (_now_iso(), card_id),
            )
            conn.execute(q, p)

    def delete_experience_card(self, card_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._user_filter("DELETE FROM experience_cards WHERE id = ?", (card_id,))
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_trade_feedback_history(self, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM trade_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Tuning Log (调优记录)
    # ------------------------------------------------------------------

    def create_tuning_proposal(self, type_: str, parameter_name: str,
                                old_value: str, new_value: str,
                                reason: str = "", evidence: list | None = None) -> str:
        tid = uuid.uuid4().hex[:12]
        conn = self._connect()
        try:
            conn.execute(
                f"""INSERT INTO tuning_log
                   (id, type, parameter_name, old_value, new_value,
                    reason, evidence, status, proposed_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (tid, type_, parameter_name, old_value, new_value,
                 reason, json.dumps(evidence or [], ensure_ascii=False),
                 "proposed", _now_iso()) + self._user_insert_params(),
            )
            conn.commit()
            return tid
        finally:
            conn.close()

    def get_tuning_proposals(self, status: str | None = None, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            if status:
                q, p = self._user_filter(
                    "SELECT * FROM tuning_log WHERE status = ? ORDER BY proposed_at DESC LIMIT ?",
                    (status, limit),
                )
                rows = conn.execute(q, p).fetchall()
            else:
                q, p = self._user_filter(
                    "SELECT * FROM tuning_log ORDER BY proposed_at DESC LIMIT ?",
                    (limit,),
                )
                rows = conn.execute(q, p).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                self._parse_json_fields(d, list_fields=("evidence",))
                result.append(d)
            return result
        finally:
            conn.close()

    def approve_tuning(self, tuning_id: str) -> bool:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "UPDATE tuning_log SET status = 'approved', decided_at = ? WHERE id = ? AND status = 'proposed'",
                (_now_iso(), tuning_id),
            )
            cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def reject_tuning(self, tuning_id: str, reason: str = "") -> bool:
        conn = self._connect()
        try:
            if reason:
                q, p = self._user_filter(
                    "UPDATE tuning_log SET status = 'rejected', decided_at = ?, reason = reason || ' | 拒绝: ' || ? WHERE id = ? AND status = 'proposed'",
                    (_now_iso(), reason, tuning_id),
                )
                cur = conn.execute(q, p)
            else:
                q, p = self._user_filter(
                    "UPDATE tuning_log SET status = 'rejected', decided_at = ? WHERE id = ? AND status = 'proposed'",
                    (_now_iso(), tuning_id),
                )
                cur = conn.execute(q, p)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Institutional Holders (机构持仓)
    # ------------------------------------------------------------------

    def save_institutional_holders(self, ticker: str, holders: list[dict]) -> int:
        """保存机构持仓数据。使用 INSERT OR REPLACE 按 (ticker, holder_name, date) 去重。"""
        if not holders:
            return 0
        with self._write_conn() as conn:
            count = 0
            for h in holders:
                conn.execute(
                    f"""INSERT OR REPLACE INTO institutional_holders
                       (ticker, holder_name, shares, value, pct_held, date, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        ticker,
                        h.get("holder_name", ""),
                        h.get("shares", 0),
                        h.get("value", 0.0),
                        h.get("pct_held", 0.0),
                        h.get("date", ""),
                        h.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count

    def get_institutional_holders(self, ticker: str, limit: int = 50) -> list[dict]:
        """获取指定 ticker 的机构持仓，按持仓比例降序。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM institutional_holders WHERE ticker = ? ORDER BY pct_held DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Analyst Ratings (分析师评级)
    # ------------------------------------------------------------------

    def save_analyst_ratings(self, ticker: str, ratings: list[dict]) -> int:
        """保存分析师评级数据。使用 INSERT OR REPLACE 按 (ticker, firm, date) 去重。"""
        if not ratings:
            return 0
        with self._write_conn() as conn:
            count = 0
            for r in ratings:
                conn.execute(
                    f"""INSERT OR REPLACE INTO analyst_ratings
                       (ticker, firm, rating, target_price, date, fetched_at{self._user_insert_cols()})
                       VALUES (?,?,?,?,?,?{self._user_insert_vals()})""",
                    (
                        ticker,
                        r.get("firm", ""),
                        r.get("rating", ""),
                        r.get("target_price"),
                        r.get("date", ""),
                        r.get("fetched_at", _now_iso()),
                    ) + self._user_insert_params(),
                )
                count += 1
            return count

    def get_analyst_ratings(self, ticker: str, limit: int = 50) -> list[dict]:
        """获取指定 ticker 的分析师评级，按日期降序。"""
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM analyst_ratings WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            )
            rows = conn.execute(q, p).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Backtest
    # ------------------------------------------------------------------

    def save_backtest_run(self, run_id: str, start_date: str, end_date: str,
                          initial_capital: float, final_equity: float,
                          total_return_pct: float, sharpe_ratio: float,
                          sortino_ratio: float, max_drawdown_pct: float,
                          calmar_ratio: float, win_rate_pct: float,
                          trade_count: int, equity_curve: list[dict]) -> None:
        with self._write_conn() as conn:
            conn.execute(
                f"""INSERT OR REPLACE INTO backtest_runs
                   (id, start_date, end_date, initial_capital, final_equity,
                    total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                    calmar_ratio, win_rate_pct, trade_count, equity_curve, created_at{self._user_insert_cols()})
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?{self._user_insert_vals()})""",
                (run_id, start_date, end_date, initial_capital, final_equity,
                 total_return_pct, sharpe_ratio, sortino_ratio, max_drawdown_pct,
                 calmar_ratio, win_rate_pct, trade_count,
                 json.dumps(equity_curve, ensure_ascii=False),
                 _now_iso()) + self._user_insert_params(),
            )

    def get_backtest_runs(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = conn.execute(q, p).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["equity_curve"] = json.loads(d.get("equity_curve", "[]"))
                except (json.JSONDecodeError, TypeError):
                    d["equity_curve"] = []
                results.append(d)
            return results
        finally:
            conn.close()

    def get_backtest_run(self, run_id: str) -> dict | None:
        conn = self._connect()
        try:
            q, p = self._user_filter(
                "SELECT * FROM backtest_runs WHERE id = ?", (run_id,)
            )
            row = conn.execute(q, p).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["equity_curve"] = json.loads(d.get("equity_curve", "[]"))
            except (json.JSONDecodeError, TypeError):
                d["equity_curve"] = []
            return d
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Helpers (通用)
    # ------------------------------------------------------------------

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
