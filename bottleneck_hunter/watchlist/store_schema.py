"""WatchlistStore 的表结构 / 索引 / 迁移 DDL。

从 store.py 抽出，纯 SQL 常量，无逻辑。由 store.py 的 _init_db 执行。
"""

from __future__ import annotations

CREATE_TABLES = """
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

CREATE TABLE IF NOT EXISTS company_profiles (
    ticker        TEXT NOT NULL,
    raw_json      TEXT DEFAULT '{}',
    sector        TEXT DEFAULT '',
    industry      TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    website       TEXT DEFAULT '',
    employees     INTEGER DEFAULT 0,
    country       TEXT DEFAULT '',
    exchange      TEXT DEFAULT '',
    currency      TEXT DEFAULT '',
    fetched_at    TEXT,
    user_id       TEXT DEFAULT '',
    PRIMARY KEY(ticker, user_id)
);

CREATE TABLE IF NOT EXISTS macro_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    indicator   TEXT NOT NULL,
    value       REAL,
    change_pct  REAL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS layer_performance (
    id              TEXT PRIMARY KEY,
    trade_id        TEXT,
    ticker          TEXT NOT NULL,
    layer           TEXT NOT NULL,           -- L1/L2/L3/L4
    score           REAL DEFAULT 0,          -- 该层归因评分(1-10 或偏差%)
    assessment      TEXT DEFAULT '',
    return_pct      REAL DEFAULT 0,          -- 本次交易最终收益%
    created_at      TEXT NOT NULL,
    user_id         TEXT DEFAULT '',
    market          TEXT DEFAULT 'us_stock'
);
"""

CREATE_INDEXES = """
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

MIGRATIONS: list[str] = [
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
    # Phase 18B: 资金操作记录表
    """CREATE TABLE IF NOT EXISTS sim_fund_ops (
        id          TEXT PRIMARY KEY,
        account_id  TEXT NOT NULL,
        op_type     TEXT NOT NULL CHECK(op_type IN ('deposit','withdraw')),
        amount      REAL NOT NULL,
        note        TEXT DEFAULT '',
        created_at  TEXT NOT NULL,
        user_id     TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_fund_ops_user ON sim_fund_ops(user_id, created_at DESC)",
    # Phase 19A: sim_trades 滑点记录
    "ALTER TABLE sim_trades ADD COLUMN slippage_bps REAL DEFAULT 0",
    # Phase 19D: experience_cards 置信度动态更新
    "ALTER TABLE experience_cards ADD COLUMN win_count INTEGER DEFAULT 0",
    "ALTER TABLE experience_cards ADD COLUMN loss_count INTEGER DEFAULT 0",
    "ALTER TABLE experience_cards ADD COLUMN last_applied_at TEXT",
    # Phase 19F: market_snapshots 数据质量标记
    "ALTER TABLE market_snapshots ADD COLUMN data_quality TEXT DEFAULT 'normal'",
    "ALTER TABLE market_snapshots ADD COLUMN quality_notes TEXT DEFAULT ''",
    # Phase 20A: 投资论点追踪系统
    """CREATE TABLE IF NOT EXISTS investment_theses (
        id              TEXT PRIMARY KEY,
        entry_id        TEXT NOT NULL,
        ticker          TEXT NOT NULL,
        thesis_title    TEXT NOT NULL,
        thesis_summary  TEXT DEFAULT '',
        conviction      TEXT DEFAULT 'medium',
        status          TEXT DEFAULT 'active',
        time_horizon    TEXT DEFAULT 'medium_term',
        created_at      TEXT NOT NULL,
        updated_at      TEXT,
        invalidated_at  TEXT,
        user_id         TEXT DEFAULT '',
        FOREIGN KEY (entry_id) REFERENCES watchlist(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS thesis_pillars (
        id              TEXT PRIMARY KEY,
        thesis_id       TEXT NOT NULL,
        pillar_text     TEXT NOT NULL,
        falsification   TEXT NOT NULL DEFAULT '',
        status          TEXT DEFAULT 'intact',
        weight          REAL DEFAULT 1.0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT,
        FOREIGN KEY (thesis_id) REFERENCES investment_theses(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS thesis_evidence_log (
        id              TEXT PRIMARY KEY,
        thesis_id       TEXT NOT NULL,
        pillar_id       TEXT,
        date            TEXT NOT NULL,
        data_point      TEXT NOT NULL,
        direction       TEXT DEFAULT 'neutral',
        thesis_impact   TEXT DEFAULT 'no_change',
        recommended_action TEXT DEFAULT 'hold',
        conviction_before TEXT DEFAULT 'medium',
        conviction_after TEXT DEFAULT 'medium',
        source          TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        user_id         TEXT DEFAULT '',
        FOREIGN KEY (thesis_id) REFERENCES investment_theses(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_thesis_entry ON investment_theses(entry_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_ticker ON investment_theses(ticker, status)",
    "CREATE INDEX IF NOT EXISTS idx_thesis_user ON investment_theses(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_pillar_thesis ON thesis_pillars(thesis_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_thesis ON thesis_evidence_log(thesis_id, date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_user ON thesis_evidence_log(user_id, date DESC)",
    # Phase 20B: 催化剂四维分类升级
    "ALTER TABLE catalyst_tracking ADD COLUMN source_category TEXT DEFAULT 'other'",
    "ALTER TABLE catalyst_tracking ADD COLUMN impact_color TEXT DEFAULT 'yellow'",
    "ALTER TABLE catalyst_tracking ADD COLUMN direction TEXT DEFAULT 'neutral'",
    "ALTER TABLE catalyst_tracking ADD COLUMN time_window TEXT DEFAULT ''",
    "ALTER TABLE catalyst_tracking ADD COLUMN position_implication TEXT DEFAULT ''",
    # Phase 20D: 三场景估值
    """CREATE TABLE IF NOT EXISTS scenario_valuations (
        id              TEXT PRIMARY KEY,
        entry_id        TEXT NOT NULL,
        ticker          TEXT NOT NULL,
        strategic_plan_id TEXT,
        bear_price      REAL,
        bear_probability REAL DEFAULT 0.2,
        bear_rationale  TEXT DEFAULT '',
        base_price      REAL,
        base_probability REAL DEFAULT 0.6,
        base_rationale  TEXT DEFAULT '',
        bull_price      REAL,
        bull_probability REAL DEFAULT 0.2,
        bull_rationale  TEXT DEFAULT '',
        current_price   REAL,
        expected_return_pct REAL DEFAULT 0,
        risk_reward_ratio REAL DEFAULT 0,
        valuation_method TEXT DEFAULT 'relative',
        created_at      TEXT NOT NULL,
        updated_at      TEXT,
        user_id         TEXT DEFAULT '',
        FOREIGN KEY (entry_id) REFERENCES watchlist(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_valuation_entry ON scenario_valuations(entry_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_valuation_ticker ON scenario_valuations(ticker, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_valuation_user ON scenario_valuations(user_id)",
    # ── Phase 21: 美股/A股市场隔离 ──
    "ALTER TABLE macro_strategies ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE strategic_plans ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE tactical_plans ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE execution_plans ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE committee_reviews ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE committee_consensus ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE sim_account ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE sim_positions ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE sim_trades ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE trade_feedback ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE auto_reviews ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE investment_theses ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE scenario_valuations ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "ALTER TABLE experience_cards ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "CREATE INDEX IF NOT EXISTS idx_macro_market ON macro_strategies(market, status)",
    "CREATE INDEX IF NOT EXISTS idx_strategic_market ON strategic_plans(market, status)",
    "CREATE INDEX IF NOT EXISTS idx_tactical_market ON tactical_plans(market, plan_date)",
    "CREATE INDEX IF NOT EXISTS idx_execution_market ON execution_plans(market, status)",
    "CREATE INDEX IF NOT EXISTS idx_sim_account_market ON sim_account(market, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sim_positions_market ON sim_positions(market, account_id)",
    "CREATE INDEX IF NOT EXISTS idx_sim_trades_market ON sim_trades(market, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trade_feedback_market ON trade_feedback(market)",
    "CREATE INDEX IF NOT EXISTS idx_auto_reviews_market ON auto_reviews(market)",
    "CREATE INDEX IF NOT EXISTS idx_theses_market ON investment_theses(market, status)",
    "CREATE INDEX IF NOT EXISTS idx_valuations_market ON scenario_valuations(market, ticker)",
    "CREATE INDEX IF NOT EXISTS idx_experience_market ON experience_cards(market)",
    "ALTER TABLE catalyst_tracking ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "CREATE INDEX IF NOT EXISTS idx_catalyst_market ON catalyst_tracking(market, status)",
    "ALTER TABLE sim_fund_ops ADD COLUMN market TEXT DEFAULT 'us_stock'",
    "CREATE INDEX IF NOT EXISTS idx_fund_ops_market ON sim_fund_ops(market)",
    # ── Phase 22A: AI 模型评分校准 + 会议历史记录 ──
    """CREATE TABLE IF NOT EXISTS model_accuracy (
        id              TEXT PRIMARY KEY,
        model_provider  TEXT NOT NULL,
        model_name      TEXT NOT NULL,
        role_context     TEXT DEFAULT '',
        ticker          TEXT NOT NULL,
        prediction_type TEXT NOT NULL,
        prediction_value TEXT NOT NULL,
        prediction_date TEXT NOT NULL,
        outcome_value   TEXT DEFAULT '',
        outcome_date    TEXT DEFAULT '',
        is_correct      INTEGER DEFAULT -1,
        score_delta     REAL DEFAULT 0,
        created_at      TEXT NOT NULL,
        updated_at      TEXT DEFAULT '',
        user_id         TEXT DEFAULT '',
        market          TEXT DEFAULT 'us_stock'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_model_acc_model ON model_accuracy(model_provider, model_name)",
    "CREATE INDEX IF NOT EXISTS idx_model_acc_ticker ON model_accuracy(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_model_acc_role ON model_accuracy(role_context)",
    "CREATE INDEX IF NOT EXISTS idx_model_acc_date ON model_accuracy(prediction_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_model_acc_user ON model_accuracy(user_id)",
    """CREATE TABLE IF NOT EXISTS model_ratings (
        id              TEXT PRIMARY KEY,
        model_provider  TEXT NOT NULL,
        model_name      TEXT NOT NULL,
        role_context     TEXT DEFAULT '',
        total_predictions INTEGER DEFAULT 0,
        correct_predictions INTEGER DEFAULT 0,
        accuracy_rate   REAL DEFAULT 0.5,
        avg_score_delta REAL DEFAULT 0,
        calibration_weight REAL DEFAULT 1.0,
        last_calibrated TEXT DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT DEFAULT '',
        user_id         TEXT DEFAULT '',
        market          TEXT DEFAULT 'us_stock'
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_model_ratings_unique ON model_ratings(model_provider, model_name, role_context, user_id, market)",
    """CREATE TABLE IF NOT EXISTS meeting_records (
        id              TEXT PRIMARY KEY,
        meeting_type    TEXT NOT NULL,
        analysis_id     TEXT DEFAULT '',
        execution_plan_id TEXT DEFAULT '',
        market          TEXT DEFAULT 'us_stock',
        title           TEXT NOT NULL,
        participants    TEXT DEFAULT '[]',
        tickers_discussed TEXT DEFAULT '[]',
        final_verdict   TEXT DEFAULT '',
        final_ranking   TEXT DEFAULT '[]',
        key_agreements  TEXT DEFAULT '[]',
        key_disagreements TEXT DEFAULT '[]',
        risk_warnings   TEXT DEFAULT '[]',
        investment_thesis TEXT DEFAULT '',
        transcript_json TEXT DEFAULT '[]',
        result_json     TEXT DEFAULT '{}',
        model_predictions TEXT DEFAULT '[]',
        duration_seconds INTEGER DEFAULT 0,
        total_tokens     INTEGER DEFAULT 0,
        created_at      TEXT NOT NULL,
        user_id         TEXT DEFAULT '',
        outcome_recorded INTEGER DEFAULT 0,
        outcome_summary TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_meeting_type ON meeting_records(meeting_type, market)",
    "CREATE INDEX IF NOT EXISTS idx_meeting_date ON meeting_records(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_meeting_user ON meeting_records(user_id)",
    # ── Phase 23: 统一 AI 配置系统 ──
    """CREATE TABLE IF NOT EXISTS ai_role_config (
        id              TEXT PRIMARY KEY,
        role_key        TEXT NOT NULL,
        role_label      TEXT NOT NULL,
        role_group      TEXT NOT NULL,
        slot_index      INTEGER DEFAULT 0,
        provider        TEXT NOT NULL,
        model           TEXT NOT NULL,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT DEFAULT '',
        user_id         TEXT DEFAULT '',
        UNIQUE(role_key, slot_index, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ai_role_config_role ON ai_role_config(role_key, user_id)",
    "CREATE INDEX IF NOT EXISTS idx_ai_role_config_group ON ai_role_config(role_group)",
    """CREATE TABLE IF NOT EXISTS model_capability_test (
        id              TEXT PRIMARY KEY,
        provider        TEXT NOT NULL,
        model           TEXT NOT NULL,
        test_type       TEXT NOT NULL,
        score           REAL DEFAULT 0,
        raw_result      TEXT DEFAULT '{}',
        tested_at       TEXT NOT NULL,
        user_id         TEXT DEFAULT '',
        UNIQUE(provider, model, test_type, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_model_cap_test ON model_capability_test(provider, model, user_id)",
    """CREATE TABLE IF NOT EXISTS model_recommendation (
        id              TEXT PRIMARY KEY,
        role_key        TEXT NOT NULL,
        slot_index      INTEGER DEFAULT 0,
        recommended_provider TEXT NOT NULL,
        recommended_model TEXT NOT NULL,
        composite_score  REAL DEFAULT 0,
        score_breakdown  TEXT DEFAULT '{}',
        reason          TEXT DEFAULT '',
        generated_at    TEXT NOT NULL,
        user_id         TEXT DEFAULT '',
        UNIQUE(role_key, slot_index, user_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_model_rec_role ON model_recommendation(role_key, user_id)",
    # ── 反向分析：从企业代码反推瓶颈环节并评分，结果持久化 ──
    """CREATE TABLE IF NOT EXISTS reverse_analyses (
        id              TEXT PRIMARY KEY,
        ticker          TEXT NOT NULL,
        company_name    TEXT DEFAULT '',
        company_name_cn TEXT DEFAULT '',
        market          TEXT DEFAULT 'us_stock',
        sector          TEXT DEFAULT '',
        bottleneck_node TEXT DEFAULT '',
        quality_score   REAL DEFAULT 0,
        alpha_score     REAL DEFAULT 0,
        final_score     REAL DEFAULT 0,
        source          TEXT DEFAULT 'llm',
        matched_analysis_id TEXT DEFAULT '',
        result_json     TEXT DEFAULT '{}',
        created_at      TEXT NOT NULL,
        updated_at      TEXT DEFAULT '',
        user_id         TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reverse_ticker ON reverse_analyses(ticker, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_reverse_market ON reverse_analyses(market, user_id)",
    # ── L1 宏观：快照记录变动率，兜底读库时不再丢失 change_pct ──
    "ALTER TABLE macro_snapshots ADD COLUMN change_pct REAL DEFAULT 0",
    # ── Phase 1.1: sim_trades 持久化已实现盈亏（卖出结算），供绩效/回测/胜率真实计算 ──
    "ALTER TABLE sim_trades ADD COLUMN realized_pnl REAL",
    # ── Phase 2.5: sim_account 记录历史权益峰值，供账户级回撤熔断 ──
    "ALTER TABLE sim_account ADD COLUMN peak_equity REAL DEFAULT 0",
]
