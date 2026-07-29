"""P2+P5 端到端：BrokerStatement → 规范表 → sim_* → 报告，含多币种基币口径 + number_guard。"""
import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult, StatementTransaction
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod

    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _stmt(*, period_end="2026-06-30", googl_mv=200000.0, tencent_mv=65440.92, etf_mv=961140.0):
    holds = [
        EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                      market_value_usd=googl_mv, nominal_ccy="USD", market_value_nominal=googl_mv),
        EquityHolding(ticker="700", company="Tencent (700 HK)", quantity=1194,
                      market_value_usd=tencent_mv, nominal_ccy="HKD", market_value_nominal=513181.20),
        EquityHolding(ticker="US4642875235", company="iShares Semiconductor ETF", quantity=1500,
                      market_value_usd=etf_mv, nominal_ccy="USD", market_value_nominal=etf_mv),
    ]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash=f"h-{period_end}-{googl_mv}", period_end=period_end,
                           holdings=holds,
                           cash_balances=[], total_cash_usd=971931.84,
                           recon=ReconResult(holdings_count=3, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def _trade_confirm_stmt():
    txns = [
        StatementTransaction(company="Alphabet Inc Dividend", txn_type="dividend", trade_date="2026-07-24",
                             net_amount=100.0, gross_amount=100.0, currency="USD", account_ref="A1",
                             external_id="t-div", isin="US02079K3059"),
        StatementTransaction(company="Cash Transfer", txn_type="deposit", trade_date="2026-07-22",
                             net_amount=500.0, gross_amount=500.0, currency="USD", account_ref="A1",
                             external_id="t-dep"),
        StatementTransaction(company="Tencent Fee", txn_type="fee", trade_date="2026-07-23",
                             net_amount=-100.0, gross_amount=-100.0, currency="HKD", account_ref="A1",
                             external_id="t-fee", isin="HK0700000000"),
    ]
    return BrokerStatement(content_hash="trade-confirm-1", period_end="2026-07-24",
                           holdings=[], transactions=txns, cash_balances=[], total_cash_usd=0.0,
                           recon=ReconResult(holdings_count=0, holdings_total_usd=0.0,
                                             statement_equities_total_usd=None, delta_usd=None,
                                             status="no_statement_total"))


def _create_doc(doc_type: str, *, user_id="u1", content_hash="x") -> str:
    from bottleneck_hunter.auth.store import AuthStore

    return AuthStore().create_financial_doc(
        user_id,
        content_hash=content_hash,
        broker="citi",
        doc_type=doc_type,
        period_end="2026-06-30",
        file_name=f"{doc_type}.pdf",
        parsed_json="{}",
        status="parsed_ok",
    )


def _set_doc_created_at(doc_id: str, created_at: str):
    from bottleneck_hunter.auth.store import AuthStore

    with AuthStore()._conn() as conn:
        conn.execute("UPDATE financial_documents SET created_at = ?, updated_at = ? WHERE id = ?",
                     (created_at, created_at, doc_id))


def test_normalize_statement_maps_isin(wl):
    r = portfolio.normalize_statement(wl, _stmt(), source_doc_id="d1", account_ref="A1")
    assert r["n_instruments"] == 3 and r["n_positions"] == 3
    conn = wl._connect()
    try:
        syms = {row["symbol"] for row in conn.execute("SELECT symbol FROM instruments").fetchall()}
    finally:
        conn.close()
    assert "SOXX" in syms and "US4642875235" not in syms
    assert "GOOGL" in syms and "700" in syms



def test_materialize_to_sim(wl):
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    m = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1",
                                        cash_total_usd=stmt.total_cash_usd)
    assert m["n_positions"] == 3
    expected = 200000.0 + 65440.92 + 961140.0 + 971931.84
    assert abs(m["total_equity"] - expected) < 1.0
    assert abs(m["cash_balance"] - 971931.84) < 1.0
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["700"]["market_value"] - 65440.92) < 1.0



def test_reparse_same_doc_to_empty_purges_stale_positions(wl):
    """重解析即权威：同一 doc 先出 3 仓，修好解析器后再解析为空 → 该 doc 旧持仓行必须被清。
    复现 CMBI 早期把 FCN 当股票落库、holdings 变空后幽灵行永久粘住的 bug。"""
    doc = _create_doc("monthly_statement", content_hash="reparse-1")
    portfolio.normalize_statement(wl, _stmt(), source_doc_id=doc, account_ref="A1")

    def _count():
        conn = wl._connect()
        try:
            return conn.execute("SELECT COUNT(*) c FROM positions WHERE source_doc_id = ?", (doc,)).fetchone()["c"]
        finally:
            conn.close()

    assert _count() == 3

    # 修好的解析器把这些头寸移出 holdings（如 FCN 归衍生品栏）→ 同 doc 重解析成空持仓
    empty = BrokerStatement(content_hash="reparse-1", period_end="2026-06-30",
                            holdings=[], cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=0, holdings_total_usd=0.0,
                                              statement_equities_total_usd=None, delta_usd=None,
                                              status="no_statement_total"))
    portfolio.normalize_statement(wl, empty, source_doc_id=doc, account_ref="A1")
    assert _count() == 0  # 幽灵行随重解析清除


def test_overview_folds_structured_product_into_holdings(wl):
    """结构性产品(FCN)走衍生品栏、不进 sim_positions，但市值已计入总权益 → 概览 holdings/
    n_holdings/集中度必须含它，否则出现'总权益百万、持仓 0 只、饼图空、集中度 0%'的错位(招银账户症状)。"""
    from bottleneck_hunter.vip import derivatives as drv
    from bottleneck_hunter.vip.derivatives import DerivativeTerm

    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1",
                                    cash_total_usd=stmt.total_cash_usd)
    base = portfolio.build_account_overview(wl, account_ref="A1")

    drv.save_derivative_term(
        wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="NVDA", currency="USD",
                           tenor_days=0,
                           terms={"market_value_usd": 250000.0, "notional": 260000.0, "maturity": "2026-09-04"}),
        source_file_name="cmbi.pdf", source_file_hash="fcn-hash", broker="cmbi", account_ref="A1")

    ov = portfolio.build_account_overview(wl, account_ref="A1")
    assert ov["n_holdings"] == base["n_holdings"] + 1
    fcn = [h for h in ov["holdings"] if h.get("kind") == "derivative"]
    assert len(fcn) == 1 and abs(fcn[0]["market_value"] - 250000.0) < 1.0

    # 无 MTM 的衍生品条款(仅有条款结构的 accumulator)不并入——其价值本就不在总权益里
    drv.save_derivative_term(
        wl, DerivativeTerm(product_family="equity_accumulator", underlying_symbol="TSLA", currency="USD",
                           tenor_days=0, terms={"afp": 300.0}),
        source_file_name="cmbi.pdf", source_file_hash="acc-hash", broker="cmbi", account_ref="A1")
    ov2 = portfolio.build_account_overview(wl, account_ref="A1")
    assert ov2["n_holdings"] == ov["n_holdings"]

    # 多期结单：同一笔 FCN(同 lot_key)再落一期(不同文件 hash) → 仍算 1 只、MTM 不翻倍
    drv.save_derivative_term(
        wl, DerivativeTerm(product_family="equity_fcn", underlying_symbol="NVDA", currency="USD",
                           tenor_days=0,
                           terms={"market_value_usd": 260000.0, "notional": 260000.0, "maturity": "2026-09-04"}),
        source_file_name="cmbi2.pdf", source_file_hash="fcn-hash-2", broker="cmbi", account_ref="A1")
    ov3 = portfolio.build_account_overview(wl, account_ref="A1")
    fcn3 = [h for h in ov3["holdings"] if h.get("kind") == "derivative"]
    assert len(fcn3) == 1 and abs(fcn3[0]["market_value"] - 260000.0) < 1.0  # 取最新一期，不叠加


def test_position_report_beats_monthly_statement_same_day(wl):
    monthly_doc = _create_doc("monthly_statement", content_hash="m1")
    pr_doc = _create_doc("position_report", content_hash="p1")

    portfolio.normalize_statement(wl, _stmt(googl_mv=200000.0), source_doc_id=monthly_doc, account_ref="A1")
    norm = portfolio.normalize_statement(wl, _stmt(googl_mv=300000.0), source_doc_id=pr_doc, account_ref="A1")
    assert norm["snapshot_applied"] is True
    assert norm["selected_doc_type"] == "position_report"

    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=971931.84)
    assert mat["selected_doc_type"] == "position_report"
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["market_value"] - 300000.0) < 1.0



def test_monthly_statement_does_not_override_position_report(wl):
    pr_doc = _create_doc("position_report", content_hash="p2")
    monthly_doc = _create_doc("monthly_statement", content_hash="m2")

    portfolio.normalize_statement(wl, _stmt(googl_mv=310000.0), source_doc_id=pr_doc, account_ref="A1")
    norm = portfolio.normalize_statement(wl, _stmt(googl_mv=180000.0), source_doc_id=monthly_doc, account_ref="A1")
    assert norm["snapshot_applied"] is False
    assert norm["selected_doc_type"] == "position_report"

    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=971931.84)
    assert mat["selected_doc_type"] == "position_report"
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["market_value"] - 310000.0) < 1.0



def test_same_type_uses_newer_created_at(wl):
    old_doc = _create_doc("monthly_statement", content_hash="m-old")
    new_doc = _create_doc("monthly_statement", content_hash="m-new")
    _set_doc_created_at(old_doc, "2026-07-01T00:00:00+00:00")
    _set_doc_created_at(new_doc, "2026-07-02T00:00:00+00:00")

    portfolio.normalize_statement(wl, _stmt(googl_mv=210000.0), source_doc_id=old_doc, account_ref="A1")
    norm = portfolio.normalize_statement(wl, _stmt(googl_mv=280000.0), source_doc_id=new_doc, account_ref="A1")
    assert norm["snapshot_applied"] is True
    assert norm["selected_doc_id"] == new_doc
    assert norm["selected_doc_type"] == "monthly_statement"

    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=971931.84)
    assert mat["selected_doc_id"] == new_doc
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["market_value"] - 280000.0) < 1.0



def test_trade_confirm_updates_transactions_not_current_holdings(wl):
    pr_doc = _create_doc("position_report", content_hash="pr-trade-1")
    trade_doc = _create_doc("trade_confirm", content_hash="tc-1")

    portfolio.normalize_statement(wl, _stmt(googl_mv=320000.0), source_doc_id=pr_doc, account_ref="A1")
    r = portfolio.normalize_statement(wl, _trade_confirm_stmt(), source_doc_id=trade_doc, account_ref="A1")
    assert r["n_positions"] == 0
    assert r["n_transactions"] == 3

    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=971931.84)
    assert mat["selected_doc_type"] == "position_report"
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["market_value"] - 320000.0) < 1.0

    overview = portfolio.build_account_overview(wl, account_ref="A1")
    assert overview["transaction_count"] == 3
    assert abs(overview["dividend_income"] - 100.0) < 0.01
    assert abs(overview["fee_total"] - 100.0) < 0.01



def test_account_overview_and_transactions(wl):
    stmt = _trade_confirm_stmt()
    r = portfolio.normalize_statement(wl, stmt, source_doc_id="d2", account_ref="A1")
    assert r["n_transactions"] == 3

    r2 = portfolio.normalize_statement(wl, stmt, source_doc_id="d2", account_ref="A1")
    assert r2["n_transactions"] == 0

    tx_all = portfolio.list_transactions(wl, account_ref="A1", limit=10)
    assert len(tx_all) == 3
    assert {row["txn_type"] for row in tx_all} == {"dividend", "deposit", "fee"}

    tx_div = portfolio.list_transactions(wl, account_ref="A1", txn_type="dividend", limit=10)
    assert len(tx_div) == 1 and tx_div[0]["txn_type"] == "dividend"

    overview = portfolio.build_account_overview(wl, account_ref="A1")
    assert overview["transaction_count"] == 3
    assert abs(overview["dividend_income"] - 100.0) < 0.01
    assert abs(overview["fee_total"] - 100.0) < 0.01
    assert abs(overview["net_inflow"] - 600.0) < 0.01
    assert abs(overview["net_outflow"] - 100.0) < 0.01
    assert overview["realized_pnl"] is None and overview["realized_pnl_available"] is False



def test_total_overview_aggregates_accounts(wl):
    stmt1 = _stmt(googl_mv=200000.0, tencent_mv=65440.92, etf_mv=0.0)
    stmt2 = _stmt(googl_mv=50000.0, tencent_mv=0.0, etf_mv=961140.0)
    portfolio.normalize_statement(wl, stmt1, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=100.0)
    portfolio.normalize_statement(wl, stmt2, account_ref="A2")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A2", cash_total_usd=200.0)

    overview = portfolio.build_total_overview(wl)
    assert abs(overview["total_equity"] - (200000.0 + 65440.92 + 50000.0 + 961140.0 + 300.0)) < 1.0
    assert abs(overview["cash_balance"] - 300.0) < 0.01
    assert overview["n_accounts"] == 2
    assert overview["n_holdings"] == 6
    assert overview["total_loan_limit"] is None
    assert {row["account_ref"] for row in overview["holdings"]} == {"A1", "A2"}
    assert {row["account_ref"] for row in overview["accounts"]} == {"A1", "A2"}
    assert abs(sum(row["total_equity"] for row in overview["accounts"]) - overview["total_equity"]) < 1.0



def test_value_series_forward_fills_across_accounts(wl):
    """两账户快照日期不齐：聚合曲线须前向填充，晚出现的账户日不得吞掉早账户的市值。"""
    wl.create_vip_account(account_ref="A1", display_name="账户1")
    wl.create_vip_account(account_ref="A2", display_name="账户2")

    def stmt(period, mv, ch):
        from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
        return BrokerStatement(
            broker="citi", period_end=period, content_hash=ch,
            holdings=[EquityHolding(ticker="AAPL", company="Apple", quantity=10, market_value_usd=mv)],
            recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                              statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))

    # A1 只在 4 月有快照，A2 只在 7 月有快照
    portfolio.normalize_statement(wl, stmt("2026-04-20", 1000.0, "a1-apr"), source_doc_id="d1", account_ref="A1")
    portfolio.normalize_statement(wl, stmt("2026-07-24", 22000.0, "a2-jul"), source_doc_id="d2", account_ref="A2")

    vs = portfolio.value_series(wl)  # 聚合
    by_date = {s["as_of_date"]: s["total_equity"] for s in vs["series"]}
    assert by_date == {"2026-04-20": 1000.0, "2026-07-24": 23000.0}  # 7 月 = A2 22000 + A1 前向填充 1000


def test_value_series_forward_fill_unit():
    """_forward_filled_series 纯函数自检：并集日期 + 各账户前向填充求和。"""
    rows = [
        {"a": "A1", "d": "2026-04-20", "eq": 1000.0},
        {"a": "A1", "d": "2026-06-30", "eq": 1200.0},
        {"a": "A2", "d": "2026-07-24", "eq": 22000.0},
    ]
    out = {s["as_of_date"]: s["total_equity"] for s in portfolio._forward_filled_series(rows)}
    assert out == {"2026-04-20": 1000.0, "2026-06-30": 1200.0, "2026-07-24": 23200.0}


def test_report_number_guard(wl):
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    summary = portfolio.build_account_summary(wl, account_ref="A1")
    assert summary["n_holdings"] == 3
    narrative = f"组合总权益 ${summary['total_equity']:,.2f}，另有臆造收益 $8,888,888.00。"
    out = portfolio.generate_vip_report(wl, period="2026-06", narrative=narrative,
                                        source_doc_ids=["d1"], account_ref="A1")
    assert "$8,888,888.00" in out["unverified"]
    assert "⚠未核到" in out["report_md"]
    assert "免责" in out["report_md"] or "声明" in out["report_md"]
    assert "持仓分析报告" in out["report_md"]



def test_report_persisted_and_audited(wl):
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    out = portfolio.generate_vip_report(wl, period="2026-06", account_ref="A1")
    conn = wl._connect()
    try:
        row = conn.execute("SELECT kind, period FROM vip_reports WHERE id=?", (out["report_id"],)).fetchone()
    finally:
        conn.close()
    assert row and row["kind"] == "periodic" and row["period"] == "2026-06"



def test_derivative_summary_in_report(wl):
    from bottleneck_hunter.vip.derivatives import DerivativeTerm

    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    terms = [
        DerivativeTerm("equity_accumulator", "MU", "USD", 365,
                       {"afp": 625.5927, "knock_out_price": 910.7569, "daily_shares": 3, "step_up_daily_shares": 6}),
        DerivativeTerm("equity_mli_booster", "MU", "USD", 120,
                       {"knock_in_pct_initial": 0.5379, "strike_pct_initial": 1.0, "max_upside_pct": 0.5}),
    ]
    out = portfolio.generate_vip_report(wl, period="2026-06", derivative_terms=terms, account_ref="A1")
    assert "衍生品 / 结构化产品风险摘要" in out["report_md"]
    assert "MU 累积器" in out["report_md"] and "MLI Booster" in out["report_md"]



def test_vip_roles_registered():
    from bottleneck_hunter.llm_clients.role_registry import get_role

    for k in ("vip_statement_extract", "vip_advisor", "vip_chat"):
        r = get_role(k)
        assert r is not None and r.group == "vip", k


@pytest.mark.asyncio
async def test_advisor_narrative_passes_number_guard(wl, monkeypatch):
    """AI 叙事：真实数字放行、编造数字标未核到；无模型时降级为纯数据报告。"""
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)

    class _FakeLLM:
        async def ainvoke(self, prompt):
            return type("R", (), {"content":
                "## 一、宏观研判\n组合总权益 $2,198,512.76，半导体高暴露。\n"
                "## 二、组合配置诊断\n前五大集中度偏高。\n"
                "## 三、操作建议\n建议减持，另有臆造回报 $7,777,777.00。"})()

    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [(_FakeLLM(), "deepseek", "deepseek-chat")])
    out = await portfolio.generate_vip_report_ai(wl, period="2026-06", user_id="u1", account_ref="A1")
    assert "AI 分析" in out["report_md"]
    assert "$7,777,777.00" in out["unverified"]
    assert "$7,777,777.00 ⚠未核到" in out["report_md"]


@pytest.mark.asyncio
async def test_advisor_no_model_degrades(wl, monkeypatch):
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [])
    out = await portfolio.generate_vip_report_ai(wl, period="2026-06", user_id="u1", account_ref="A1")
    assert out["report_id"] and "持仓分析报告" in out["report_md"]


def test_startup_purges_empty_account_ref_residue(tmp_path, monkeypatch):
    """历史 hidden default / 空 ref 残留在下次 init 时被一次性清除，且不留孤儿 sim_*。"""
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    db = tmp_path / "wl.db"

    wl = WatchlistStore(db).for_user("u1").for_market("us_stock")
    conn = wl._connect()
    try:
        conn.execute("INSERT INTO vip_accounts(id, account_ref, display_name, user_id, market, created_at, updated_at) "
                     "VALUES('a0','','默认账户','u1','us_stock','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO sim_account(id, account_ref, user_id, market, created_at) "
                     "VALUES('s0','','u1','us_stock','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO sim_positions(id, account_id, ticker, opened_at, user_id, market) "
                     "VALUES('p0','s0','GOOGL','2026-01-01T00:00:00Z','u1','us_stock')")
        conn.execute("INSERT INTO positions(id, instrument_id, account_ref, as_of_date, created_at, user_id, market) "
                     "VALUES('pp0','inst0','','2026-01-01','2026-01-01T00:00:00Z','u1','us_stock')")
        conn.commit()
    finally:
        conn.close()

    # 重新打开同一 DB → 触发迁移清理
    WatchlistStore(db)
    conn = WatchlistStore(db)._connect()
    try:
        for tbl, col in [("vip_accounts", "account_ref"), ("sim_account", "account_ref"),
                         ("positions", "account_ref")]:
            n = conn.execute(f"SELECT COUNT(*) c FROM {tbl} WHERE COALESCE({col},'')=''").fetchone()["c"]
            assert n == 0, tbl
        assert conn.execute("SELECT COUNT(*) c FROM sim_positions WHERE account_id='s0'").fetchone()["c"] == 0
    finally:
        conn.close()


def _misparse_stmt(*, period_end, mv=5000.0):
    """误读单：仅 1 只小额持仓（模拟误判券商/部分解析后的垃圾结果）。"""
    h = EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=5,
                      market_value_usd=mv, nominal_ccy="USD", market_value_nominal=mv)
    return BrokerStatement(content_hash=f"bad-{period_end}-{mv}", period_end=period_end,
                           holdings=[h], cash_balances=[], total_cash_usd=0.0,
                           recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                                             statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))


def test_guard_blocks_stale_statement(wl):
    # 先入库较新的 6-30 快照
    good = _stmt(period_end="2026-06-30")
    portfolio.normalize_statement(wl, good, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1",
                                    cash_total_usd=good.total_cash_usd)
    equity_before = wl.get_sim_account(account_ref="A1")["total_equity"]

    # 再导入更旧的 4-30 快照 → 应被陈旧护栏拦下，sim live 不动
    old = _stmt(period_end="2026-04-30", googl_mv=1.0)
    portfolio.normalize_statement(wl, old, account_ref="A1")
    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-04-30", account_ref="A1",
                                          cash_total_usd=old.total_cash_usd)
    assert mat.get("guard_skipped", "").startswith("stale_snapshot")
    assert abs(wl.get_sim_account(account_ref="A1")["total_equity"] - equity_before) < 1.0
    # 旧持仓仍留在规范层（历史留痕，未被清）
    assert mat["n_positions"] == 0


def test_guard_blocks_shrink_misparse(wl):
    good = _stmt(period_end="2026-06-30")
    portfolio.normalize_statement(wl, good, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1",
                                    cash_total_usd=good.total_cash_usd)
    acct_before = wl.get_sim_account(account_ref="A1")
    n_before = len(wl.get_sim_positions(acct_before["id"]))
    assert n_before == 3

    # 更新的日期(不触发陈旧)、但总值/持仓骤降 → 骤降护栏拦下
    bad = _misparse_stmt(period_end="2026-07-31")
    portfolio.normalize_statement(wl, bad, account_ref="A1")
    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-07-31", account_ref="A1",
                                          cash_total_usd=bad.total_cash_usd)
    assert mat.get("guard_skipped", "").startswith("suspected_misparse")
    acct_after = wl.get_sim_account(account_ref="A1")
    assert abs(acct_after["total_equity"] - acct_before["total_equity"]) < 1.0
    assert len(wl.get_sim_positions(acct_after["id"])) == 3


def test_guard_allows_normal_newer_update(wl):
    jun = _stmt(period_end="2026-06-30", googl_mv=200000.0)
    portfolio.normalize_statement(wl, jun, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1",
                                    cash_total_usd=jun.total_cash_usd)

    # 正常更新：更新日期 + 量级相当 → 放行，sim 反映新快照
    jul = _stmt(period_end="2026-07-31", googl_mv=250000.0)
    portfolio.normalize_statement(wl, jul, account_ref="A1")
    mat = portfolio.materialize_portfolio(wl, as_of_date="2026-07-31", account_ref="A1",
                                          cash_total_usd=jul.total_cash_usd)
    assert "guard_skipped" not in mat
    assert mat["n_positions"] == 3
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["market_value"] - 250000.0) < 1.0
