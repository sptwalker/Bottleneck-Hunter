"""SQLite persistence for the watchlist tracking system.

Follows the same patterns as ``dataflows/store.py``:
raw SQL, WAL mode, ``sqlite3.Row`` factory, ``_MIGRATIONS`` list.
Separate DB file ``data/watchlist.db`` to avoid migration conflicts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
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
"""

_MIGRATIONS: list[str] = []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Store class
# ---------------------------------------------------------------------------

class WatchlistStore:
    def __init__(self, db_path: str | Path | None = None):
        self._db_path = str(db_path or _DEFAULT_DB)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
            row = conn.execute("SELECT COUNT(*) AS cnt FROM watchlist WHERE tier = ?", (tier,)).fetchone()
            if row["cnt"] >= self._TIER_LIMITS.get(tier, 12):
                raise ValueError(f"Tier '{tier}' is full (max {self._TIER_LIMITS[tier]})")
            total = conn.execute("SELECT COUNT(*) AS cnt FROM watchlist").fetchone()
            if total["cnt"] >= 24:
                raise ValueError("Watchlist is full (max 24)")
            # 检查重复
            existing = conn.execute("SELECT id FROM watchlist WHERE ticker = ?", (entry["ticker"],)).fetchone()
            if existing:
                raise ValueError(f"Ticker '{entry['ticker']}' already in watchlist")

            entry_id = entry.get("id") or uuid.uuid4().hex[:12]
            now = _now_iso()
            conn.execute(
                """INSERT INTO watchlist
                   (id, ticker, company_name, company_name_cn, market, tier, tier_rank,
                    composite_score, source, source_analysis_id, sector, bottleneck_node,
                    added_at, updated_at, notes, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                ),
            )
            conn.commit()
            return entry_id
        finally:
            conn.close()

    def remove(self, entry_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM watchlist WHERE id = ?", (entry_id,))
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
            cur = conn.execute(f"UPDATE watchlist SET {', '.join(parts)} WHERE id = ?", vals)
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM watchlist WHERE id = ?", (entry_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_by_ticker(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_all(self, tier: str | None = None) -> list[dict]:
        conn = self._connect()
        try:
            if tier:
                rows = conn.execute(
                    "SELECT * FROM watchlist WHERE tier = ? ORDER BY composite_score DESC, tier_rank ASC",
                    (tier,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watchlist ORDER BY tier, composite_score DESC, tier_rank ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count_by_tier(self) -> dict[str, int]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT tier, COUNT(*) AS cnt FROM watchlist GROUP BY tier").fetchall()
            result = {"focus": 0, "normal": 0, "track": 0}
            for r in rows:
                result[r["tier"]] = r["cnt"]
            return result
        finally:
            conn.close()

    def get_tickers(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT ticker FROM watchlist WHERE is_active = 1").fetchall()
            return [r["ticker"] for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Market Snapshots
    # ------------------------------------------------------------------

    def save_snapshots(self, snapshots: list[dict]) -> int:
        if not snapshots:
            return 0
        conn = self._connect()
        try:
            count = 0
            for s in snapshots:
                conn.execute(
                    """INSERT OR REPLACE INTO market_snapshots
                       (ticker, date, open, high, low, close, volume, market_cap,
                        pe_ratio, change_pct, rsi_14, macd, macd_signal, macd_hist,
                        sma_20, sma_50, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        s["ticker"], s["date"], s.get("open"), s.get("high"),
                        s.get("low"), s.get("close"), s.get("volume"),
                        s.get("market_cap"), s.get("pe_ratio"), s.get("change_pct"),
                        s.get("rsi_14"), s.get("macd"), s.get("macd_signal"),
                        s.get("macd_hist"), s.get("sma_20"), s.get("sma_50"),
                        s.get("fetched_at", _now_iso()),
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_snapshots(self, ticker: str, days: int = 90) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, days),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_latest_snapshot(self, ticker: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM market_snapshots WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
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
                    """INSERT OR IGNORE INTO news_digest
                       (id, ticker, date, title, summary, sentiment, sentiment_score,
                        source_url, source_name, llm_analysis, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        nid, n["ticker"], n["date"], n["title"],
                        n.get("summary", ""), n.get("sentiment", ""),
                        n.get("sentiment_score", 0.0), n.get("source_url", ""),
                        n.get("source_name", ""), n.get("llm_analysis", ""),
                        n.get("fetched_at", _now_iso()),
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_news(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM news_digest WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
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
                    """INSERT OR IGNORE INTO sec_filings
                       (id, ticker, filing_type, filed_date, title, summary, url,
                        is_insider_trade, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        fid, f["ticker"], f["filing_type"], f["filed_date"],
                        f.get("title", ""), f.get("summary", ""), f.get("url", ""),
                        1 if f.get("is_insider_trade") else 0,
                        f.get("fetched_at", _now_iso()),
                    ),
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
                rows = conn.execute(
                    "SELECT * FROM sec_filings WHERE ticker = ? AND filing_type = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, filing_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sec_filings WHERE ticker = ? ORDER BY filed_date DESC LIMIT ?",
                    (ticker, limit),
                ).fetchall()
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
                    """INSERT OR IGNORE INTO insider_trades
                       (id, ticker, insider_name, insider_title, transaction_type,
                        shares, price, total_value, date, source_filing_id, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        tid, t["ticker"], t["insider_name"], t.get("insider_title", ""),
                        t.get("transaction_type", ""), t.get("shares", 0),
                        t.get("price"), t.get("total_value"), t["date"],
                        t.get("source_filing_id", ""), t.get("fetched_at", _now_iso()),
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_insider_trades(self, ticker: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM insider_trades WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
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
                    """INSERT OR IGNORE INTO options_activity
                       (id, ticker, date, unusual_volume, put_call_ratio,
                        total_call_volume, total_put_volume, max_oi_strike,
                        max_oi_expiry, notable_trades, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        aid, a["ticker"], a["date"],
                        1 if a.get("unusual_volume") else 0,
                        a.get("put_call_ratio"), a.get("total_call_volume", 0),
                        a.get("total_put_volume", 0), a.get("max_oi_strike"),
                        a.get("max_oi_expiry", ""),
                        json.dumps(a.get("notable_trades", []), ensure_ascii=False),
                        a.get("fetched_at", _now_iso()),
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_options(self, ticker: str, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM options_activity WHERE ticker = ? ORDER BY date DESC LIMIT ?",
                (ticker, limit),
            ).fetchall()
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
                    """INSERT OR IGNORE INTO earnings_reports
                       (id, ticker, report_date, fiscal_quarter, eps_actual,
                        eps_estimate, eps_surprise_pct, revenue_actual,
                        revenue_estimate, guidance, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        eid, e["ticker"], e["report_date"],
                        e.get("fiscal_quarter", ""),
                        e.get("eps_actual"), e.get("eps_estimate"),
                        e.get("eps_surprise_pct"), e.get("revenue_actual"),
                        e.get("revenue_estimate"), e.get("guidance", ""),
                        e.get("fetched_at", _now_iso()),
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_earnings(self, ticker: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM earnings_reports WHERE ticker = ? ORDER BY report_date DESC",
                (ticker,),
            ).fetchall()
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
                """INSERT INTO llm_budget
                   (date, provider, model, input_tokens, output_tokens, estimated_cost_usd, task_type)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    usage.get("date", _today()),
                    usage.get("provider", ""), usage.get("model", ""),
                    usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                    usage.get("estimated_cost_usd", 0.0), usage.get("task_type", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_daily_usage(self, date: str | None = None) -> dict:
        date = date or _today()
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date = ?""",
                (date,),
            ).fetchone()
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
            row = conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(estimated_cost_usd),0.0) AS cost
                   FROM llm_budget WHERE date LIKE ?""",
                (f"{prefix}%",),
            ).fetchone()
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
            rows = conn.execute("SELECT key, value FROM budget_config").fetchall()
            return {r["key"]: float(r["value"]) for r in rows}
        finally:
            conn.close()

    def set_budget_limit(self, key: str, value: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO budget_config(key, value) VALUES (?, ?)",
                (key, str(value)),
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
                "INSERT OR IGNORE INTO pipeline_status(pipeline_name) VALUES (?)", (name,)
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
                conn.execute(
                    f"UPDATE pipeline_status SET {', '.join(parts)} WHERE pipeline_name = ?", vals
                )
            conn.commit()
        finally:
            conn.close()

    def get_pipeline_statuses(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM pipeline_status").fetchall()
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
                """INSERT INTO uzi_analyses
                   (id, entry_id, ticker, analysis_type, status, started_at)
                   VALUES (?, ?, ?, ?, 'running', ?)""",
                (analysis_id, entry_id, ticker, analysis_type, _now_iso()),
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
            conn.execute(
                """UPDATE uzi_analyses SET
                   status='completed', completed_at=?, result_json=?,
                   summary=?, score=?, signal=?, trap_level=?
                   WHERE id=?""",
                (_now_iso(), result_json, summary, score, signal,
                 trap_level, analysis_id),
            )
            conn.commit()
        finally:
            conn.close()

    def fail_uzi_analysis(self, analysis_id: str, error: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE uzi_analyses SET
                   status='failed', completed_at=?, summary=?
                   WHERE id=?""",
                (_now_iso(), error, analysis_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_uzi_history(self, entry_id: str,
                        limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, entry_id, ticker, analysis_type, status,
                   started_at, completed_at, summary, score, signal, trap_level
                   FROM uzi_analyses WHERE entry_id = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (entry_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_uzi_analysis(self, analysis_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM uzi_analyses WHERE id = ?", (analysis_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
