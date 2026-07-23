"""P2+P5 端到端：BrokerStatement → 规范表 → sim_* → 报告，含多币种基币口径 + number_guard。"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult


@pytest.fixture
def wl(tmp_path):
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _stmt():
    holds = [
        EquityHolding(ticker="GOOGL", company="Alphabet Inc", quantity=100,
                      market_value_usd=200000.0, nominal_ccy="USD", market_value_nominal=200000.0),
        EquityHolding(ticker="700", company="Tencent (700 HK)", quantity=1194,
                      market_value_usd=65440.92, nominal_ccy="HKD", market_value_nominal=513181.20),
        EquityHolding(ticker="US4642875235", company="iShares Semiconductor ETF", quantity=1500,
                      market_value_usd=961140.0, nominal_ccy="USD", market_value_nominal=961140.0),
    ]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash="h1", period_end="2026-06-30", holdings=holds,
                           recon=ReconResult(holdings_count=3, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def test_normalize_writes_regularized(wl):
    r = portfolio.normalize_statement(wl, _stmt(), source_doc_id="d1", account_ref="A1")
    assert r["n_instruments"] == 3 and r["n_positions"] == 3
    # ETF ISIN → 可交易代码
    conn = wl._connect()
    try:
        syms = {row["symbol"] for row in conn.execute("SELECT symbol FROM instruments").fetchall()}
    finally:
        conn.close()
    assert "SOXX" in syms and "US4642875235" not in syms   # ISIN 已映射
    assert "GOOGL" in syms and "700" in syms


def test_materialize_to_sim(wl):
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    m = portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1")
    assert m["n_positions"] == 3
    # 总权益 = 统一美元基币合计（港股用 $65,440 而非 HKD 513,181）
    assert abs(m["total_equity"] - (200000.0 + 65440.92 + 961140.0)) < 1.0
    # sim_positions 落地
    acct = wl.get_sim_account()
    pos = {p["ticker"]: p for p in wl.get_sim_positions(acct["id"])}
    assert abs(pos["700"]["market_value"] - 65440.92) < 1.0   # 基币口径，非 HKD


def test_generate_report_with_number_guard(wl):
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1")
    summary = portfolio.build_portfolio_summary(wl)
    assert summary["n_holdings"] == 3
    # 叙事含一个真实值 + 一个编造值
    narrative = f"组合总权益 ${summary['total_equity']:,.2f}，另有臆造收益 $8,888,888.00。"
    out = portfolio.generate_vip_report(wl, period="2026-06", narrative=narrative,
                                        source_doc_ids=["d1"])
    assert "$8,888,888.00" in out["unverified"]      # 编造被抓
    assert "⚠未核到" in out["report_md"]
    assert "免责" in out["report_md"] or "声明" in out["report_md"]  # 挂了免责
    assert "持仓分析报告" in out["report_md"]


def test_report_persisted_and_audited(wl):
    portfolio.normalize_statement(wl, _stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, account_ref="A1")
    out = portfolio.generate_vip_report(wl, period="2026-06")
    conn = wl._connect()
    try:
        row = conn.execute("SELECT kind, period FROM vip_reports WHERE id=?", (out["report_id"],)).fetchone()
    finally:
        conn.close()
    assert row and row["kind"] == "periodic" and row["period"] == "2026-06"
