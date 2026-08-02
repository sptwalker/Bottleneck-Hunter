"""Phase 2 回归守卫：收益侧地基。

P2-1：单账户价值曲线口径统一为「结单权威净值(含现金·净融资)」——与头条 total_equity 同口径，
      消除"曲线不含现金 vs 头条含现金"的同屏分裂；缺权威净值才回落持仓市值口径并如实标 basis。
P2-2：外部现金流(注资/提取/转入转出)与买卖交割在流水聚合层严格分离——TWR/MWR 分母只应剔外部现金流；
      net_inflow/net_outflow 保留旧「按符号全额」语义(向后兼容)。
"""
import json

import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _one_holding_stmt(*, ticker="AAPL", mv=100000.0, cash=0.0, period_end="2026-06-30", ch="h1"):
    return BrokerStatement(
        content_hash=ch, period_end=period_end,
        holdings=[EquityHolding(ticker=ticker, company=ticker, quantity=10,
                                market_value_usd=mv, nominal_ccy="USD", market_value_nominal=mv)],
        cash_balances=[], total_cash_usd=cash,
        recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                          statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))


def _imp(wl, iid, pe, te, account_ref, created="2026-07-01T00:00:00+00:00"):
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
            "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (iid, f"{iid}.pdf", iid, "pdf", "monthly_statement", "imported",
             json.dumps({"period_end": pe, "total_equity": te}), account_ref, created, "u1", "us_stock"))


def _imp_period_only(wl, iid, pe, account_ref, created="2026-07-01T00:00:00+00:00"):
    """结单期已导入但未落 total_equity（旧导入）：_import_total_series 应过滤掉、_latest_import_period 仍取 pe。"""
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_imports(id,file_name,file_hash,file_type,detected_kind,status,"
            "key_metrics_json,account_ref,created_at,user_id,market) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (iid, f"{iid}.pdf", iid, "pdf", "monthly_statement", "imported",
             json.dumps({"period_end": pe}), account_ref, created, "u1", "us_stock"))


def _deriv(wl, account_ref, mv, *, fam="equity_fcn", sym="NVDA", ch="d1"):
    with wl._write_conn() as conn:
        conn.execute(
            "INSERT INTO vip_derivative_terms(id,source_file_name,source_file_hash,broker,product_family,"
            "underlying_symbol,currency,terms_json,rationale_ref,account_ref,lot_key,created_at,user_id,market)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ch, f"{ch}.pdf", ch, "cmbi", fam, sym, "USD",
             json.dumps({"market_value_usd": mv}), "", account_ref, "", "2026-07-01T00:00:00+00:00",
             "u1", "us_stock"))


# ── P2-2：外部现金流与买卖交割分离（纯函数自检） ──────────────────────────

def test_overview_totals_separates_external_cashflow():
    """deposit/transfer_in 计 external_inflow；withdrawal/transfer_out 计 external_outflow；
    买卖交割不混入外部现金流；net_inflow/net_outflow 仍是旧「按符号全额」语义(向后兼容)。"""
    rows = [
        {"txn_type": "dividend", "net_amount": 100.0},
        {"txn_type": "deposit", "net_amount": 500.0},
        {"txn_type": "fee", "net_amount": -100.0},
        {"txn_type": "withdrawal", "net_amount": -300.0},
        {"txn_type": "transfer_in", "net_amount": 200.0},
        {"txn_type": "transfer_out", "net_amount": -50.0},
        {"txn_type": "buy", "net_amount": -1000.0},
        {"txn_type": "sell", "net_amount": 800.0},
    ]
    t = portfolio._overview_totals(rows)
    # 外部现金流分离
    assert t["external_inflow"] == 700.0        # deposit 500 + transfer_in 200
    assert t["external_outflow"] == 350.0       # withdrawal 300 + transfer_out 50
    assert t["net_external_cashflow"] == 350.0
    assert t["external_txn_count"] == 4
    # 买卖交割不被当作外部现金流
    assert t["buy_amount"] == 1000.0 and t["sell_amount"] == 800.0
    # 旧口径不变（向后兼容/其他测试锁定）
    assert t["net_inflow"] == 1600.0            # 所有正额：div100+dep500+ti200+sell800
    assert t["net_outflow"] == 1450.0           # 所有负额绝对值：fee100+wd300+to50+buy1000


def test_overview_totals_no_external_when_only_trades():
    """招银这类只出 buy/sell 的月结单：external_txn_count=0 表示未逐笔披露转账，非"无外部现金流"。"""
    rows = [{"txn_type": "buy", "net_amount": -1000.0}, {"txn_type": "sell", "net_amount": 1200.0}]
    t = portfolio._overview_totals(rows)
    assert t["external_inflow"] == 0.0 and t["external_outflow"] == 0.0
    assert t["net_external_cashflow"] == 0.0 and t["external_txn_count"] == 0


# ── P2-1：曲线口径 = 结单权威净值(含现金)，缺则回落持仓市值 ──────────────

def test_value_series_prefers_authoritative_total_over_positions(wl):
    """账户既有持仓快照(市值和 100000，不含现金)又有结单权威净值(130000，含现金)：
    曲线取权威净值、basis=authoritative_total_equity，与头条同口径，绝不用持仓市值和。"""
    portfolio.normalize_statement(wl, _one_holding_stmt(mv=100000.0, cash=30000.0), account_ref="MIX")
    portfolio.materialize_portfolio(wl, account_ref="MIX", cash_total_usd=30000.0)
    _imp(wl, "im1", "2026-06-30", 130000.0, "MIX")   # 权威净值 = 持仓 100000 + 现金 30000

    vs = portfolio.value_series(wl, account_ref="MIX")
    assert vs["basis"] == "authoritative_total_equity"
    assert len(vs["series"]) == 1
    assert vs["series"][0]["total_equity"] == 130000.0   # 含现金口径，非持仓市值和 100000


def test_value_series_falls_back_to_positions_when_no_authoritative(wl):
    """无结单权威净值(仅持仓导出/旧导入未落 total_equity)：回落持仓市值口径并如实标 basis。"""
    portfolio.normalize_statement(wl, _one_holding_stmt(ticker="MSFT", mv=50000.0, cash=9999.0), account_ref="POS")
    portfolio.materialize_portfolio(wl, account_ref="POS", cash_total_usd=9999.0)

    vs = portfolio.value_series(wl, account_ref="POS")
    assert vs["basis"] == "positions_market_value"
    assert len(vs["series"]) == 1
    assert vs["series"][0]["total_equity"] == 50000.0    # 持仓市值和（不含现金）


def test_overview_totals_negative_when_only_outflow():
    """只出提取/转出：net_external_cashflow 为负，方向不被 abs 抹平（TWR/MWR 分母符号正确）。"""
    rows = [{"txn_type": "withdrawal", "net_amount": -800.0},
            {"txn_type": "transfer_out", "net_amount": -200.0}]
    t = portfolio._overview_totals(rows)
    assert t["external_inflow"] == 0.0
    assert t["external_outflow"] == 1000.0
    assert t["net_external_cashflow"] == -1000.0        # 净流出为负，非 +1000
    assert t["external_txn_count"] == 2


def test_perf_summary_basis_reflects_caliber():
    """basis 标注随曲线口径如实切换——含现金/衍生品MTM/持仓市值三态各不相同，绝不混用分母口径。"""
    def caliber(b):
        return portfolio._perf_summary({"series": [], "basis": b}, {}, 0.0)["basis"]
    assert "含现金" in caliber("authoritative_total_equity")
    assert "衍生品" in caliber("derivative_mtm_anchor")
    assert "不含现金" in caliber("positions_market_value")   # 缺省/持仓口径


def test_value_series_derivative_mtm_anchor_when_no_positions_no_authoritative(wl):
    """纯结构性产品账户：无股票快照、无权威净值，退回单点当期 MTM 锚点，basis=derivative_mtm_anchor，曲线不空白。"""
    _imp_period_only(wl, "imp0", "2026-06-30", "DRV")    # 有期末日但未落 total_equity → 权威序列为空
    _deriv(wl, "DRV", 88000.0)                            # 当期 MTM 88000

    vs = portfolio.value_series(wl, account_ref="DRV")
    assert vs["basis"] == "derivative_mtm_anchor"
    assert len(vs["series"]) == 1
    assert vs["series"][0]["total_equity"] == 88000.0
    assert vs["series"][0]["as_of_date"] == "2026-06-30"


def test_value_series_projection_point_lifted_to_cash_inclusive_under_authoritative(wl):
    """红线不变量守卫：权威净值口径(含现金)下叠加系统推算点时，末点必须仍是含现金口径(权威净值+股票重估增量)，
    绝不把末点打回持仓市值口径——否则曲线末点≠头条、派生虚假末期收益/回撤(P2-1 回归)。"""
    portfolio.normalize_statement(wl, _one_holding_stmt(mv=100000.0, cash=30000.0), account_ref="MIX")
    portfolio.materialize_portfolio(wl, account_ref="MIX", cash_total_usd=30000.0)
    _imp(wl, "im1", "2026-06-30", 130000.0, "MIX")       # 权威净值 130000 = 持仓 100000 + 现金 30000
    # 更新一日的股票推算：AAPL 由 100000 重估到 110000（+10000）
    wl.upsert_projection(account_ref="MIX", as_of_date="2026-07-15", kind="stock_mtm",
                         ticker="AAPL", market_value_base=110000.0)

    vs = portfolio.value_series(wl, account_ref="MIX")
    assert vs["basis"] == "authoritative_total_equity"
    assert len(vs["series"]) == 2
    tail = vs["series"][-1]
    assert tail.get("is_projected") is True
    # 末点 = 权威净值 130000 + 股票重估增量 (110000−100000) = 140000（含现金口径），绝非纯持仓 110000
    assert tail["total_equity"] == 140000.0
    assert tail["total_equity"] != 110000.0


def test_importer_side_map_normalizes_withdrawal_to_canonical(wl):
    """根因守卫：CSV/Excel 提取行必须规范为 transactions.txn_type CHECK 合法的 'withdrawal'——
    半词 'withdraw' 会撞 CHECK 抛 IntegrityError 令整单导入失败、且被外部现金流聚合漏计。"""
    from bottleneck_hunter.vip import importer
    from bottleneck_hunter.vip.ingest import BrokerStatement, ReconResult, StatementTransaction
    # 生产端：所有提取别名一律规范为 'withdrawal'
    for alias in ("withdraw", "withdrawal", "出金", "转出"):
        assert importer._SIDE_MAP[alias] == "withdrawal"
    # 消费端：规范值可直插 transactions(过 CHECK)，且被外部现金流正确归为流出
    stmt = BrokerStatement(
        broker="generic", content_hash="wd1",
        transactions=[StatementTransaction(company="Cash Out", txn_type="withdrawal",
                                           trade_date="2026-07-10", net_amount=-500.0, currency="USD")],
        recon=ReconResult(holdings_count=0, holdings_total_usd=0.0,
                          statement_equities_total_usd=None, delta_usd=None, status="no_statement_total"))
    portfolio.normalize_statement(wl, stmt, source_doc_id="wd:1", account_ref="WD")  # 不抛 IntegrityError
    rows = portfolio.list_transactions(wl, account_ref="WD")
    assert [r["txn_type"] for r in rows] == ["withdrawal"]
    t = portfolio._overview_totals(rows)
    assert t["external_outflow"] == 500.0 and t["net_external_cashflow"] == -500.0
