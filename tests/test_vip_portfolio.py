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


def _create_doc(doc_type: str, *, user_id="u1", content_hash="x", period_end="2026-06-30") -> str:
    from bottleneck_hunter.auth.store import AuthStore

    return AuthStore().create_financial_doc(
        user_id,
        content_hash=content_hash,
        broker="citi",
        doc_type=doc_type,
        period_end=period_end,
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



def test_cost_carried_forward_when_latest_snapshot_lacks_cost(wl):
    """最新快照(仓盘导出)无成本列 → 按 symbol 结转前期带成本快照，用当前市值重算未实现盈亏。
    修「所有子账户未实现收益全为 0」根因。"""
    # 前期(05-31)：带成本的完整结单，GOOGL 100股、成本基 180000、市值 200000
    prior = _create_doc("monthly_statement", content_hash="prior", period_end="2026-05-31")
    prior_hold = EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                               market_value_usd=200000.0, nominal_ccy="USD", market_value_nominal=200000.0,
                               avg_cost=1800.0, cost_basis_usd=180000.0, unrealized_pnl_usd=20000.0)
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="prior", period_end="2026-05-31", holdings=[prior_hold],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=200000.0,
                                              statement_equities_total_usd=200000.0, delta_usd=0.0, status="ok")),
        source_doc_id=prior, account_ref="A1")

    # 本期(06-30)：仓盘导出，无成本列（cost 全 None），GOOGL 仍 100 股、市值涨到 260000
    cur = _create_doc("position_report", content_hash="cur", period_end="2026-06-30")
    cur_hold = EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                             market_value_usd=260000.0, nominal_ccy="USD", market_value_nominal=260000.0)
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="cur", period_end="2026-06-30", holdings=[cur_hold],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=260000.0,
                                              statement_equities_total_usd=260000.0, delta_usd=0.0, status="ok")),
        source_doc_id=cur, account_ref="A1")

    cm = portfolio._canonical_cost_map(wl, "A1")
    g = cm["GOOGL"]
    assert g["cost_basis"] == 180000.0                    # 成本自 05-31 结转
    assert g["cost_carried_from"] == "2026-05-31"         # 披露结转来源
    assert g["unrealized_pnl"] == 80000.0                 # 当前市值260000 − 历史成本180000（非旧 20000）
    assert g["unrealized_pnl_pct"] == round(80000.0 / 180000.0 * 100, 2)


def test_materialize_carries_cost_into_sim_positions(wl):
    """成本结转必须穿透到 sim_positions（「持仓」标签页 /account/positions 读的表），
    否则概览层已修、持仓表仍全 0 无红绿。修「未实现盈亏全为0又复发」根因。"""
    prior = _create_doc("monthly_statement", content_hash="mp-prior", period_end="2026-05-31")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="mp-prior", period_end="2026-05-31",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                                                    market_value_usd=200000.0, nominal_ccy="USD",
                                                    market_value_nominal=200000.0, avg_cost=1800.0,
                                                    cost_basis_usd=180000.0, unrealized_pnl_usd=20000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=200000.0,
                                              statement_equities_total_usd=200000.0, delta_usd=0.0, status="ok")),
        source_doc_id=prior, account_ref="A1")
    # 本期仓盘导出无成本、股数未变、市值涨到 260000
    cur = _create_doc("position_report", content_hash="mp-cur", period_end="2026-06-30")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="mp-cur", period_end="2026-06-30",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                                                    market_value_usd=260000.0, nominal_ccy="USD",
                                                    market_value_nominal=260000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=260000.0,
                                              statement_equities_total_usd=260000.0, delta_usd=0.0, status="ok")),
        source_doc_id=cur, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=0.0)
    acct = wl.get_sim_account(account_ref="A1")
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["GOOGL"]["unrealized_pnl"] - 80000.0) < 1.0   # 结转成本后非 0 → 前端上绿色


def test_cost_not_carried_when_quantity_changed(wl):
    """股数变动(买卖)后旧成本基失真 → 不结转、诚实留 None，不臆造未实现盈亏。"""
    prior = _create_doc("monthly_statement", content_hash="q-prior", period_end="2026-05-31")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="q-prior", period_end="2026-05-31",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                                                    market_value_usd=200000.0, nominal_ccy="USD",
                                                    market_value_nominal=200000.0, avg_cost=1800.0,
                                                    cost_basis_usd=180000.0, unrealized_pnl_usd=20000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=200000.0,
                                              statement_equities_total_usd=200000.0, delta_usd=0.0, status="ok")),
        source_doc_id=prior, account_ref="A1")
    # 本期股数翻倍(加仓) + 无成本
    cur = _create_doc("position_report", content_hash="q-cur", period_end="2026-06-30")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="q-cur", period_end="2026-06-30",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=200,
                                                    market_value_usd=520000.0, nominal_ccy="USD",
                                                    market_value_nominal=520000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=520000.0,
                                              statement_equities_total_usd=520000.0, delta_usd=0.0, status="ok")),
        source_doc_id=cur, account_ref="A1")
    g = portfolio._canonical_cost_map(wl, "A1")["GOOGL"]
    assert g["cost_basis"] is None and g["unrealized_pnl"] is None   # 股数变→不结转，诚实留空
    assert g["cost_carried_from"] is None


def test_backfill_heals_stale_sim_without_reimport(wl):
    """B1：成本结转能对【存量】sim 主动回填——不必重传结算单。
    复现根因：sim 在成本结转修复【之前】物化 → 行冻结为无成本(avg_cost=现价、pnl=0)；改码不回写旧行，
    页面永远读到 0。backfill_account_cost 每次重估前跑一次即自愈，且不覆盖已有真成本。"""
    prior = _create_doc("monthly_statement", content_hash="bf-prior", period_end="2026-05-31")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="bf-prior", period_end="2026-05-31",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                                                    market_value_usd=200000.0, nominal_ccy="USD",
                                                    market_value_nominal=200000.0, avg_cost=1800.0,
                                                    cost_basis_usd=180000.0, unrealized_pnl_usd=20000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=200000.0,
                                              statement_equities_total_usd=200000.0, delta_usd=0.0, status="ok")),
        source_doc_id=prior, account_ref="A1")
    cur = _create_doc("position_report", content_hash="bf-cur", period_end="2026-06-30")
    portfolio.normalize_statement(
        wl, BrokerStatement(content_hash="bf-cur", period_end="2026-06-30",
                            holdings=[EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                                                    market_value_usd=260000.0, nominal_ccy="USD",
                                                    market_value_nominal=260000.0)],
                            cash_balances=[], total_cash_usd=0.0,
                            recon=ReconResult(holdings_count=1, holdings_total_usd=260000.0,
                                              statement_equities_total_usd=260000.0, delta_usd=0.0, status="ok")),
        source_doc_id=cur, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=0.0)
    acct = wl.get_sim_account(account_ref="A1")
    pos0 = wl.get_sim_positions(acct["id"])[0]
    # 模拟"修复前"物化：把 sim 成本抹成=现价、盈亏抹成 0（冻结的无成本行）
    wl.update_sim_position(pos0["id"], avg_cost=pos0["current_price"], unrealized_pnl=0.0)
    assert wl.get_sim_positions(acct["id"])[0]["unrealized_pnl"] == 0.0

    n = portfolio.backfill_account_cost(wl, "A1")
    assert n == 1
    healed = wl.get_sim_positions(acct["id"])[0]
    assert abs(healed["unrealized_pnl"] - 80000.0) < 1.0    # 市值260000 − 结转成本180000 → 前端绿色

    # 幂等 + 不覆盖真成本：再跑一次不应改动（已是真成本，had_cost 分支跳过）
    assert portfolio.backfill_account_cost(wl, "A1") == 0


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
    assert overview["total_loan"] == 0.0
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


def test_nomura_summary_unreliable_guard():
    """野村 +17 偏移错列 → 产出不可能的账户级摘要(NAV≤0 / 负债>总资产)。判据须拦下真实坏值、放行正常账户。
    拦下后 _parse_nomura_summary 会把 NAV/负债/贷款清零 → 下游回落「持仓+现金」算净值、不写幽灵贷款。"""
    from bottleneck_hunter.vip.ingest import _nomura_summary_unreliable as u
    # 5 期真实野村坏值（实测 account_summary），均须判不可信
    assert u(-8984893, 0, 0)
    assert u(-8760666, 6638865, 15399530)
    assert u(-8245586, 7012811, 15258397)
    assert u(-9011526, 0, 15578584)
    assert u(-9485263, 6250061, 15735325)
    # 正常账户放行：NAV>0 且 负债≤总资产（含有真实融资的场景）
    assert not u(1942658.0, 1942658.0, 0.0)
    assert not u(1942658.0, 2000000.0, 500000.0)


def test_value_series_forward_fill_unit():
    """_forward_filled_series 纯函数自检：并集日期 + 各账户前向填充求和。"""
    rows = [
        {"a": "A1", "d": "2026-04-20", "eq": 1000.0},
        {"a": "A1", "d": "2026-06-30", "eq": 1200.0},
        {"a": "A2", "d": "2026-07-24", "eq": 22000.0},
    ]
    out = {s["as_of_date"]: s["total_equity"] for s in portfolio._forward_filled_series(rows)}
    assert out == {"2026-04-20": 1000.0, "2026-06-30": 1200.0, "2026-07-24": 23200.0}


def test_value_series_derivative_only_account_uses_import_totals(wl):
    """纯结构性产品账户(无 positions)：价值曲线用各期结单权威净值(vip_imports.total_equity)建多点，
    逐期收益率随之可算；同一期多次导入取最新一次(created_at 最大)的 total_equity，旧导入无该键的期不成点。"""
    import json

    def _imp(iid, pe, te, created):
        with wl._write_conn() as conn:
            conn.execute(
                "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
                "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (iid, f"{iid}.pdf", iid, "pdf", "monthly_statement", "imported",
                 json.dumps({"period_end": pe, "total_equity": te}), "DERIV", created, "u1", "us_stock"))

    _imp("i1", "2026-04-30", 1000000.0, "2026-07-01T00:00:00+00:00")
    _imp("i2", "2026-05-31", 1010000.0, "2026-07-02T00:00:00+00:00")
    _imp("i3", "2026-05-31", 1020000.0, "2026-07-03T00:00:00+00:00")  # 同期重导 → 覆盖为最新净值
    _imp("i4", "", None, "2026-07-04T00:00:00+00:00")                 # 缺期/缺净值 → 不成点

    vs = portfolio.value_series(wl, account_ref="DERIV")
    by = {s["as_of_date"]: s["total_equity"] for s in vs["series"]}
    assert list(by) == ["2026-04-30", "2026-05-31"]
    assert by["2026-05-31"] == 1020000.0          # 取同期最新一次导入
    assert len(vs["returns"]) == 1                # 两点 → 一段逐期收益率
    assert abs(vs["returns"][0]["pct"] - 2.0) < 0.01  # (1020000-1000000)/1000000


def test_value_series_merges_partial_authoritative_with_positions(wl):
    """回归:部分期有权威净值(仅最新期落 total_equity,旧期迁移前导入未落)——曾整段用权威净值替换
    更长的持仓市值历史,曲线塌成「1 权威点+1 推算点」=2 点(野村账户症状)。修法:按期合并,有权威用权威、
    缺的期回落持仓市值,曲线保持连续。断言:多持仓期全保留 + 最新期取权威净值(含现金) + basis 标混合。"""
    import json

    def stmt(period, mv, ch):
        from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
        return BrokerStatement(
            broker="nomura", period_end=period, content_hash=ch,
            holdings=[EquityHolding(ticker="AAPL", company="Apple", quantity=10, market_value_usd=mv)],
            recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                              statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))

    # 三期持仓快照（模拟野村本地已有的多期 positions）
    portfolio.normalize_statement(wl, stmt("2026-04-20", 1000.0, "n-apr"), source_doc_id="d1", account_ref="NOMURA")
    portfolio.normalize_statement(wl, stmt("2026-06-30", 1200.0, "n-jun"), source_doc_id="d2", account_ref="NOMURA")
    portfolio.normalize_statement(wl, stmt("2026-07-31", 1500.0, "n-jul"), source_doc_id="d3", account_ref="NOMURA")

    # 仅最新一期落权威净值（含现金 → 高于持仓市值），模拟服务器新导入态
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
            "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("n1", "n1.pdf", "n1", "pdf", "monthly_statement", "imported",
             json.dumps({"period_end": "2026-07-31", "total_equity": 5000.0}),
             "NOMURA", "2026-07-31T00:00:00+00:00", "u1", "us_stock"))

    vs = portfolio.value_series(wl, account_ref="NOMURA")
    real = [s for s in vs["series"] if not s.get("is_projected")]
    by = {s["as_of_date"]: s["total_equity"] for s in real}
    assert vs["basis"] == "mixed_authoritative_and_positions"
    assert set(by) == {"2026-04-20", "2026-06-30", "2026-07-31"}  # 三期全保留，未塌成 2 点
    assert by["2026-04-20"] == 1000.0 and by["2026-06-30"] == 1200.0  # 缺权威 → 持仓市值
    assert by["2026-07-31"] == 5000.0                                # 有权威 → 权威净值(含现金)


def test_holdings_as_of_falls_back_to_statement_period_for_derivative_only(wl):
    """§1 staleness 锚点:纯衍生品账户 positions 空 → _holdings_as_of 回落最新结单期末(非空串)。
    /account/staleness 端点改用此 helper,故此断言锁死"招银 CMBIS 导入后校准日 = 结单期末"而非硬查空 positions。"""
    import json

    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
            "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("s1", "s1.pdf", "s1", "pdf", "monthly_statement", "imported",
             json.dumps({"period_end": "2026-07-31", "total_equity": 1063096.5}),
             "CMBIS", "2026-07-31T00:00:00+00:00", "u1", "us_stock"))

    assert portfolio._holdings_as_of(wl, "CMBIS") == "2026-07-31"   # positions 空 → 结单期末


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
    assert "衍生品 / 结构性产品" in out["report_md"]
    assert "MU 累积器" in out["report_md"] and "增益票据" in out["report_md"]


def test_derivative_indicative_excluded_from_all_read_paths(wl):
    """(e) 推介稿(is_indicative=1)不得出现在报告/持仓任何读取路径——标记 + 读时过滤。"""
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term

    save_derivative_term(wl, DerivativeTerm("equity_accumulator", "MU", "USD", 365,
                                            {"afp": 625.59, "market_value_usd": 999999.0,
                                             "maturity": "2099-12-31"}),
                         source_file_name="Indicative Terms MU.pdf", source_file_hash="h1",
                         broker="citi", account_ref="A1", lot_key="625.5927:991231",
                         is_indicative=True)
    save_derivative_term(wl, DerivativeTerm("equity_accumulator", "ORCL", "USD", 365,
                                            {"afp": 100.0, "market_value_usd": 12345.0,
                                             "maturity": "2099-12-31"}),
                         source_file_name="termsheet_orcl.pdf", source_file_hash="h2",
                         broker="citi", account_ref="A1", lot_key="100:991231")

    from bottleneck_hunter.vip import derivatives as drv
    listed = drv.list_derivative_terms(wl, account_ref="A1")
    assert [t.underlying_symbol for t in listed] == ["ORCL"]
    acc = drv.list_derivative_terms_all_accounts(wl, account_ref="A1")
    assert [i["underlying_symbol"] for i in acc] == ["ORCL"]
    cur = portfolio._current_derivative_rows(wl, "A1")
    assert [r["underlying_symbol"] for r in cur] == ["ORCL"]
    assert portfolio._derivative_mtm_total(wl, "A1") == 12345.0  # 推介稿的 999999 不计入 MTM


def test_migrate_flag_indicative_backfill(wl):
    """(e) 历史脏行一次性标记：推介稿(文件名/无成交字段+无lot_key)→1；真单(lot_key)→0 不误杀；幂等。"""
    from bottleneck_hunter.vip.derivatives import DerivativeTerm, save_derivative_term

    save_derivative_term(wl, DerivativeTerm("equity_fcn", "CMBI", "USD", 60, {"maturity": "2026-09-04"}),
                         source_file_name="Indicative Terms CMBI.pdf", source_file_hash="h1",
                         broker="citi", account_ref="A1")
    save_derivative_term(wl, DerivativeTerm("equity_fcn", "CMBI", "USD", 60, {"maturity": "2026-09-04"}),
                         source_file_name="CMBI_termsheet.docx", source_file_hash="h2",
                         broker="cmbi", account_ref="A1", lot_key="XS3372957897:2026-09-04")

    conn = wl._connect()
    try:
        wl._migrate_flag_indicative_derivative_terms(conn)
        flags = {r["source_file_name"]: r["is_indicative"]
                 for r in conn.execute("SELECT source_file_name, is_indicative FROM vip_derivative_terms").fetchall()}
        wl._migrate_flag_indicative_derivative_terms(conn)  # 幂等重放
        flags2 = {r["source_file_name"]: r["is_indicative"]
                  for r in conn.execute("SELECT source_file_name, is_indicative FROM vip_derivative_terms").fetchall()}
    finally:
        conn.close()
    assert flags["Indicative Terms CMBI.pdf"] == 1
    assert flags["CMBI_termsheet.docx"] == 0      # 有 lot_key(ISIN:到期) 不误杀
    assert flags2 == flags                          # 幂等


def test_report_anchors_persisted(wl):
    """(b) 双数据锚点：生成时落 payload_json，报告 HTML 显示双时间戳。"""
    import json

    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    out = portfolio.generate_vip_report(wl, period="2026-06", account_ref="A1")
    conn = wl._connect()
    try:
        row = conn.execute("SELECT payload_json FROM vip_reports WHERE id=?", (out["report_id"],)).fetchone()
    finally:
        conn.close()
    anchors = json.loads(row["payload_json"])["data_anchors"]
    assert anchors["holdings_as_of"] == "2026-06-30"
    assert "market_as_of" in anchors
    assert "账户数据 <b>2026-06-30</b>" in out["report_md"]


def test_report_html_plain_no_jargon(wl):
    """(d) HTML 图文报告：ECharts 占位 + 通俗措辞 + 去专业术语。"""
    stmt = _stmt()
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1", cash_total_usd=stmt.total_cash_usd)
    out = portfolio.generate_vip_report(wl, period="2026-06", account_ref="A1")
    md = out["report_md"]
    assert "data-chart='holdings-pie'" in md          # ECharts 占位 div
    assert "<div class='vip-report-cards'>" in md     # HTML 概览卡片
    assert "vip-report-tbl" in md                     # 持仓明细表
    assert "Modified Dietz" not in md and "归因" not in md  # 去专业术语



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


def test_startup_preserves_empty_account_ref(tmp_path, monkeypatch):
    """回归守卫：account_ref='' 是决策中心合法业务键，重开库绝不能清除它。
    曾有启动迁移 _migrate_purge_empty_account_ref 无条件清空 → 每次重启丢决策模拟账户，
    已作为数据丢失根因移除（见 memory project_dc_sim_account_decoupled_from_vip）。此测反向锁死该修复。"""
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    db = tmp_path / "wl.db"

    wl = WatchlistStore(db).for_user("u1").for_market("us_stock")
    conn = wl._connect()
    try:
        conn.execute("INSERT INTO sim_account(id, account_ref, user_id, market, created_at) "
                     "VALUES('s0','','u1','us_stock','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO sim_positions(id, account_id, ticker, opened_at, user_id, market) "
                     "VALUES('p0','s0','GOOGL','2026-01-01T00:00:00Z','u1','us_stock')")
        conn.commit()
    finally:
        conn.close()

    # 重新打开同一 DB → 不得触发任何空 account_ref 清除
    WatchlistStore(db)
    conn = WatchlistStore(db)._connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM sim_account WHERE COALESCE(account_ref,'')=''").fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) c FROM sim_positions WHERE account_id='s0'").fetchone()["c"] == 1
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


# ── P0/P1 币种敞口 + FX 落库 ──────────────────────────────────────────────

def test_fx_rate_and_nominal_persisted_from_statement(wl):
    """P0：多币种结单落库后，positions.market_value 存原币市值、fx_rate 存隐含汇率（非恒 1.0）。"""
    portfolio.normalize_statement(wl, _stmt(), source_doc_id="d1", account_ref="A1")
    conn = wl._connect()
    try:
        rows = {r["symbol"]: r for r in conn.execute(
            "SELECT i.symbol, p.market_value AS nom, p.market_value_base AS usd, p.fx_rate, p.market_price "
            "FROM positions p JOIN instruments i ON i.id = p.instrument_id").fetchall()}
    finally:
        conn.close()
    # 港股 Tencent：原币市值 513181.20 HKD / 美元市值 65440.92 → fx ≈ 0.1275
    hk = rows["700"]
    assert abs(hk["nom"] - 513181.20) < 1.0          # market_value = 原币市值(非美元镜像)
    assert abs(hk["usd"] - 65440.92) < 1.0
    assert abs(hk["fx_rate"] - (65440.92 / 513181.20)) < 1e-4
    assert hk["fx_rate"] < 0.5                          # 港币 fx 明显 != 1.0
    assert abs(hk["market_price"] - 513181.20 / 1194) < 1e-3   # 原币单价
    # 美元标的 GOOGL：原币==美元、fx==1.0
    us = rows["GOOGL"]
    assert abs(us["nom"] - us["usd"]) < 1e-6 and abs(us["fx_rate"] - 1.0) < 1e-6


def test_exposure_breakdown_by_currency_detail(wl):
    """P1：_exposure_breakdown 产出 by_currency_detail，含美元/原币/隐含汇率。"""
    portfolio.normalize_statement(wl, _stmt(), source_doc_id="d1", account_ref="A1")
    exp = portfolio._exposure_breakdown(wl, "A1")
    detail = {d["currency"]: d for d in exp["by_currency_detail"]}
    assert set(detail) == {"USD", "HKD"}
    hkd = detail["HKD"]
    assert abs(hkd["market_value_usd"] - 65440.92) < 1.0
    assert abs(hkd["market_value_nominal"] - 513181.20) < 1.0
    assert abs(hkd["implied_fx"] - (65440.92 / 513181.20)) < 1e-4
    usd = detail["USD"]     # GOOGL 200000 + SOXX 961140 同币聚合
    assert abs(usd["market_value_nominal"] - usd["market_value_usd"]) < 1e-6
    assert abs(usd["implied_fx"] - 1.0) < 1e-6


def test_overview_surfaces_exposure_breakdown(wl):
    """P1 接线：build_account_overview 把 exposure_breakdown 挂进返回体（前端币种饼取数前提）。"""
    portfolio.normalize_statement(wl, _stmt(), source_doc_id="d1", account_ref="A1")
    ov = portfolio.build_account_overview(wl, account_ref="A1")
    ccys = {d["currency"] for d in ov["exposure_breakdown"]["by_currency_detail"]}
    assert ccys == {"USD", "HKD"}


# ── P2 汇率损益归因 ───────────────────────────────────────────────────────

def _hk_stmt(*, period_end, hk_nom, hk_fx):
    """单港股账户结单：Tencent 原币市值 hk_nom @ 隐含汇率 hk_fx（qty 恒定，供 P2 相邻两期归因）。"""
    h = EquityHolding(ticker="700", company="Tencent (700 HK)", quantity=1000,
                      market_value_usd=round(hk_nom * hk_fx, 2), nominal_ccy="HKD",
                      market_value_nominal=hk_nom)
    total = h.market_value_usd
    return BrokerStatement(content_hash=f"hk-{period_end}", period_end=period_end, holdings=[h],
                           cash_balances=[], total_cash_usd=0.0,
                           recon=ReconResult(holdings_count=1, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def test_fx_attribution_wired_into_dossier(wl):
    """P2 接线：相邻两期本币 +5%、港币贬 2% → dossier.fx_attribution 拆出 r_local/r_fx/乘性 total。"""
    fx0 = 0.128
    portfolio.normalize_statement(wl, _hk_stmt(period_end="2026-06-30", hk_nom=100000.0, hk_fx=fx0),
                                  source_doc_id="p0", account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=0.0)
    # 期末：原币 +5%(100000→105000)、港币贬 2%(fx*0.98)
    portfolio.normalize_statement(wl, _hk_stmt(period_end="2026-07-31", hk_nom=105000.0, hk_fx=fx0 * 0.98),
                                  source_doc_id="p1", account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-07-31", account_ref="A1", cash_total_usd=0.0)

    dossier = portfolio.build_account_dossier(wl, account_ref="A1")
    fx = dossier["fx_attribution"]
    assert fx["prev_date"] == "2026-06-30" and fx["cur_date"] == "2026-07-31"
    row = {r["symbol"]: r for r in fx["rows"]}["700"]
    assert abs(row["r_local_pct"] - 5.0) < 0.05, row       # 本币价 +5%
    assert abs(row["r_fx_pct"] - (-2.0)) < 0.05, row       # 港币贬 2%
    assert abs(row["total_pct"] - 2.9) < 0.05, row         # 乘性 (1.05*0.98-1)=+2.9%
    assert fx["coverage"] == "1/1"                          # fx 锚齐全


# ── 股票/基金读时分类 + company_profile 负缓存 + 概览冷却窗 ───────────────────

def test_classify_asset_class():
    """读时派生分类(不写库、不依赖会超时的 yfinance)：instrument_type / 名称 token /
    精选基金 ticker / 美国共同基金形态(5位大写以X结尾) → fund；否则 stock。"""
    c = portfolio.classify_asset_class
    assert c("GLD") == "fund"                                   # 精选基金 ticker
    assert c("AAPL") == "stock"                                 # 普通股
    assert c("XYZ", "iShares MSCI Emerging Markets ETF") == "fund"   # 名称 token
    assert c("VFIAX") == "fund"                                 # 共同基金形态
    assert c("ANYTHING", "", "etf") == "fund"                   # instrument_type 权威
    assert c("MSFT", "Microsoft Corporation") == "stock"        # 普通名股票
    assert c("700", "Tencent Holdings Ltd") == "stock"          # 港股正常股票


def test_fund_is_exchange_traded():
    """场内(可取行情K线) vs 场外(仅结算单净值)形态判别：A股 15/5x ETF 与美股 GLD/SPY/QQQ → 场内；
    欧洲 ISIN / 美国开放式共同基金(5位X) / A股非ETF前缀(如 00 开头开放式) → 场外。"""
    f = portfolio.fund_is_exchange_traded
    assert f("GLD") is True and f("SPY") is True and f("QQQ") is True  # 美股场内 ETF
    assert f("IE00SYNTH001") is False              # 欧洲 ISIN 形态场外基金：无公开行情源
    assert f("VFIAX") is False                     # 美国开放式共同基金(5位X)
    assert f("510300", "a_stock") is True          # 沪 ETF(51 前缀)
    assert f("159919", "a_stock") is True          # 深 ETF(15 前缀)
    assert f("000001", "a_stock") is False         # A股场外开放式基金(00 前缀)
    assert f("") is False


def test_company_overview_isin_gate():
    """company-overview 对 ISIN 形态跳过 yfinance 按需抓取（无公开源→避免 429/错配垃圾行情/.info 超时）；
    GLD 场内 ETF、A股/美股普通股票不被误跳过（保留正常抓取）。gate 由 _ISIN_RE 判定驱动。"""
    from bottleneck_hunter.web.watchlist_api import _ISIN_RE
    assert _ISIN_RE.match("IE00SYNTH001")                                  # 欧洲 ISIN 场外基金 → 跳过按需抓取
    assert not _ISIN_RE.match("GLD") and not _ISIN_RE.match("SPY")         # 场内 ETF 保留 K 线抓取
    assert not _ISIN_RE.match("AAPL") and not _ISIN_RE.match("600519.SH")  # 普通美股 / A股股票保留抓取


def test_fund_nav_series(wl):
    """场外基金净值走势：各期结算单 市值/份额 连点。两期 → 两点，nav=mv/qty，按日升序。"""
    wl.create_vip_account(account_ref="A1", display_name="账户1")

    def isin_stmt(period, mv, ch):
        return BrokerStatement(
            broker="citi", period_end=period, content_hash=ch,
            holdings=[EquityHolding(ticker="IE00SYNTH001", company="某场外基金",
                                    quantity=1000, market_value_usd=mv)],
            recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                              statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))

    portfolio.normalize_statement(wl, isin_stmt("2026-05-31", 100000.0, "nav-may"),
                                  source_doc_id="nav-d1", account_ref="A1")
    portfolio.normalize_statement(wl, isin_stmt("2026-06-30", 110000.0, "nav-jun"),
                                  source_doc_id="nav-d2", account_ref="A1")

    out = portfolio.fund_nav_series(wl, account_ref="A1", symbol="IE00SYNTH001")
    assert out["basis"] == "statement_valuation"
    assert [s["as_of_date"] for s in out["series"]] == ["2026-05-31", "2026-06-30"]  # 升序
    assert {s["as_of_date"]: s["nav"] for s in out["series"]} == {"2026-05-31": 100.0, "2026-06-30": 110.0}
    assert portfolio.fund_nav_series(wl, account_ref="A1", symbol="NOPE")["series"] == []  # 无此标的→空


def test_company_profile_negative_cache_non_clobber(wl):
    """空 info → 落负缓存 stub(带 fetched_at, raw 空)；真 info 覆盖之；再空 info 绝不覆盖真资料。
    根治 GLD 等 .info 反复超时/429 时空结果永不落库 → 每次打开抽屉都重拉的洪流。"""
    wl.save_company_profile("GLD", {})
    stub = wl.get_company_profile("GLD")
    assert stub is not None and stub["fetched_at"] and stub["raw"] == {}   # stub 有时刻、raw 空

    wl.save_company_profile("GLD", {"sector": "Financial", "quoteType": "ETF",
                                    "longBusinessSummary": "Gold ETF"})
    real = wl.get_company_profile("GLD")
    assert real["raw"]["quoteType"] == "ETF"                    # 真 info 覆盖 stub

    wl.save_company_profile("GLD", {})                          # 再抓空 → 不得覆盖真资料
    assert wl.get_company_profile("GLD")["raw"]["quoteType"] == "ETF"


def test_profile_is_stale():
    from datetime import datetime, timedelta, timezone

    from bottleneck_hunter.web.watchlist_api import _profile_is_stale

    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
    fresh = {"fetched_at": (now - timedelta(hours=1)).isoformat()}
    stale = {"fetched_at": (now - timedelta(hours=25)).isoformat()}
    assert _profile_is_stale(fresh, now) is False              # 冷却窗内 → 新鲜
    assert _profile_is_stale(stale, now) is True               # 超 24h → 过期，允许重拉一次
    assert _profile_is_stale(None, now) is True                # 无 profile → 拉
    assert _profile_is_stale({"fetched_at": ""}, now) is True   # 无时刻 → 拉


def test_instruments_by_ticker_carries_source_doc(wl):
    """_instruments_by_ticker 返回 (instrument_type, name, source_doc_id)——供 /account/positions
    给基金持仓溯源结算单文件名(source_doc_id → auth.db financial_documents.file_name)。"""
    portfolio._upsert_instrument(wl, "GLD", "etf", "SPDR Gold Shares", "USD", "doc1")
    imap = portfolio._instruments_by_ticker(wl)
    assert imap["GLD"] == ("etf", "SPDR Gold Shares", "doc1")


def test_refresh_staleness_gate_and_all_account_projection(wl):
    """即时刷新核心逻辑：新鲜度门控 + 全账户重估。GOOGL/SOXX 为美股形态持仓(700=HK 数字码被排除)。

    - 收盘价日=应有交易日 → stale 为空(走"已最新"分支，不打网络)；
    - 收盘价日为上周 → 对应标的进 stale；
    - project_all_accounts_mtm 对每个账户各写一条推算(验证"全账户覆盖"，非只刷打开的那个)。
    """
    from bottleneck_hunter.vip import projection

    # 两个账户各持一份(含 GOOGL/SOXX)——覆盖"全账户"而非单账户
    for ref in ("A1", "A2"):
        portfolio.normalize_statement(wl, _stmt(), account_ref=ref)
        portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref=ref, cash_total_usd=0.0)

    syms = projection._account_priceable_symbols(wl)
    assert syms == {"GOOGL", "SOXX"}   # 700(HK 数字码)不入美股补价集；ISIN ETF 已归一为 SOXX

    expected = projection._expected_snapshot_date("us_stock")
    # 应有交易日当天两只都有收盘价 → 不 stale
    wl.save_snapshots([{"ticker": "GOOGL", "date": expected, "close": 190.0, "market": "us_stock"},
                       {"ticker": "SOXX", "date": expected, "close": 640.0, "market": "us_stock"}])
    assert projection.stale_symbols(wl, syms, "us_stock") == []
    # 删掉 GOOGL 的新鲜快照，只留 SOXX 新鲜 → 仅 GOOGL 进 stale（验证逐标的判据）
    with wl._write_conn() as conn:
        conn.execute("DELETE FROM market_snapshots WHERE ticker='GOOGL' AND date=?", (expected,))
    assert projection.stale_symbols(wl, syms, "us_stock") == ["GOOGL"]

    # 全账户重估：2 个账户各写 GOOGL/SOXX 推算(用留存的收盘价，幂等)
    res = projection.project_all_accounts_mtm(wl)
    assert res["accounts"] == 2
    for ref in ("A1", "A2"):
        tickers = {r["ticker"] for r in wl.list_projections(account_ref=ref, kind="stock_mtm")}
        assert "SOXX" in tickers, f"账户 {ref} 未写入 SOXX 推算"

