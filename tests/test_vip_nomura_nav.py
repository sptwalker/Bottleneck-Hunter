"""A: 野村结单完整账户口径 —— NAV 锚覆盖 sim_account.total_equity。"""
from pathlib import Path

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.vip import ingest, portfolio

REAL = Path(r"C:\Users\walker\Documents\walker\银行文件\野村结单\22704339_Statement.pdf")


@pytest.mark.skipif(not REAL.exists(), reason="真实野村结单不存在")
def test_nomura_nav_drives_total_equity(tmp_path):
    wl = WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")
    stmt = ingest.ingest_pdf(REAL.read_bytes(), REAL.name, broker_hint="nomura", pdf_password="22704339")
    portfolio.normalize_statement(wl, stmt, source_doc_id="d1", account_ref="22704339.001")
    m = portfolio.materialize_portfolio(
        wl, as_of_date=stmt.period_end, account_ref="22704339.001",
        cash_total_usd=stmt.total_cash_usd,
        account_total_usd=stmt.account_summary["net_asset_value_usd"],
    )
    # 完整账户口径：总权益用 NAV，不是 股票+现金
    assert m["cash_balance"] == stmt.total_cash_usd
    assert m["total_equity"] == stmt.account_summary["net_asset_value_usd"]
    rep = portfolio.generate_vip_report(wl, period="2026-06")
    assert "组合总权益" in rep["report_md"] and "可投资现金" in rep["report_md"]
