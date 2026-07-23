"""Nomura 结单解析：密码 PDF / 现金+权益结构 / broker 检测。"""
from pathlib import Path

import pytest

from bottleneck_hunter.vip import ingest


def _make_nomura_like_pdf() -> bytes:
    import fitz
    lines = [
        "BANK COPY",
        "Account Number: 22704339",
        "As Of Date: 02-JUN-2026",
        "Portfolio Statement",
        "Reference currency: USD",
        "Nomura Singapore Limited 10 Marina Boulevard ...",
        "Position Details − Money Account",
        "Description", "Value", "(in original currency)", "Value", "(USD)",
        "Cash", "Money Accounts",
        "HKD", "4,378,236.46", "558,591.03",
        "USD", "4,502,813.70", "4,502,813.70",
        "Total", "5,061,404.73",
        "Position Details − Equities",
        "Description", "Quantity", "Market Value", "Value (USD)",
        "HKD",
        "TENCENT HOLDINGS LTD (700 HK)",
        "Sector − Communication",
        "KYG875721634",
        "s",
        "55",
        "380.00",
        "481.60",
        "26,488.00",
        "699.65",
        "3,379.43",
        "0.17",
        "7.799148",
        "02.06.2026",
        "USD",
        "BLOOM ENERGY CORP− A (BE US)",
        "Sector − Industrial",
        "US0937121079",
        "s",
        "131",
        "255.47",
        "270.65",
        "35,444.52",
        "1,990.00",
        "35,444.52",
        "0.18",
        "1.000000",
        "02.06.2026",
    ]
    doc = fitz.open(); pg = doc.new_page(); pg.insert_text((36,40), '\n'.join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def nomura_pdf():
    return _make_nomura_like_pdf()


def test_detect_broker_nomura(nomura_pdf):
    pages = ingest._extract_pages(nomura_pdf)
    assert ingest.detect_broker(pages, filename="Statement_260602.pdf") == "nomura"


def test_parse_nomura_statement(nomura_pdf):
    stmt = ingest.ingest_pdf(nomura_pdf, "Statement_260602.pdf", broker_hint="nomura")
    assert stmt.broker == "nomura"
    assert stmt.period_end == "2026-06-02"
    assert abs(stmt.total_cash_usd - 5061404.73) < 0.01
    by = {c.currency: c for c in stmt.cash_balances}
    assert abs(by["HKD"].market_value_usd - 558591.03) < 0.01
    assert abs(by["USD"].market_value_usd - 4502813.70) < 0.01
    syms = {h.ticker for h in stmt.holdings}
    assert syms == {"700", "BE"}
    hk = next(h for h in stmt.holdings if h.ticker == "700")
    assert abs(hk.market_value_usd - 3379.43) < 0.01
    us = next(h for h in stmt.holdings if h.ticker == "BE")
    assert abs(us.market_value_usd - 35444.52) < 0.01
    # 合成样本不带 Performance Summary / NAV 段，故结单层总额未知，按 no_statement_total 处理
    assert stmt.recon.status == "no_statement_total"


@pytest.mark.skipif(not Path(r"C:\Users\walker\Documents\walker\银行文件\野村结单\22704339_Statement.pdf").exists(), reason="真实样本不存在")
def test_real_nomura_statement_smoke():
    p = Path(r"C:\Users\walker\Documents\walker\银行文件\野村结单\22704339_Statement.pdf")
    stmt = ingest.ingest_pdf(p.read_bytes(), p.name, broker_hint="nomura", pdf_password="22704339")
    assert stmt.broker == "nomura"
    assert stmt.holdings and stmt.cash_balances and stmt.total_cash_usd > 0
