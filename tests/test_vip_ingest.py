"""P1 摄取管道：解析 / 对账 / 期末日 / 幂等去重 / 加密落库。

用合成 PDF（fitz 造，复刻花旗行偏移格式）验证解析，无需真实私密月结单。
"""
import pytest

from bottleneck_hunter.vip import ingest


def _make_citi_like_pdf() -> bytes:
    """造一个花旗 EQUITIES 行格式的 PDF：每只持仓块为
    [数量, 单价, 总成本, 现价, 市值, 未实现, 总值, 公司名, 日期, 3行占位, Ticker行]
    与真实 fitz 抽取的相对偏移一致：Ticker 行往前 i-10=数量, i-6=市值, i-3=公司名。
    """
    import fitz
    # 一只持仓从 Ticker 行往前的 10 行（i-10..i-1）+ Ticker 行
    def block(qty, mv, company):
        return [
            f"{qty:,.0f}",        # i-10 数量
            "185.1907",           # i-9  单价
            "627,240.95",         # i-8  总成本
            "357.37",             # i-7  现价
            f"{mv:,.2f}",         # i-6  市值 ★
            "583,171.24",         # i-5  未实现
            f"{mv:,.2f}",         # i-4  总值
            company,              # i-3  公司名 ★
            "30JUN26",            # i-2  日期
            "3.35",               # i-1  %
            f"Ticker {company_ticker[company]} UW Equity",  # i 行 ★
        ]
    company_ticker = {"Alphabet Inc": "GOOGL", "Meta Platforms Inc": "META"}

    def block_ccy(qty, mv_nominal, mv_usd, company, anchor):
        """非 USD / ETF：市值原币(i-6) ≠ 美元总值(i-4)，锚行可为 Ticker 或 ISIN。"""
        return [
            f"{qty:,.0f}", "317.3527", "378,919.12", "429.8",
            f"{mv_nominal:,.2f}",     # i-6 市值(原币)
            "134,262.08",
            f"{mv_usd:,.2f}",         # i-4 Total Value USD
            company, "30JUN26", "0.18", anchor,
        ]

    lines = ["INVESTMENT POSITIONS", "EQUITIES 60.86% SORTED BY NOM CCY",
             "Nominal Ccy", "Quantity", "Description", "Market Value"]
    lines += block(3387, 1210412.19, "Alphabet Inc")
    lines += block(2292, 1291060.68, "Meta Platforms Inc")
    # ETF：ISIN 锚（无 Ticker 行）
    lines += ["Developed Large Cap Equities (USD)"]
    lines += block_ccy(1500, 961140.00, 961140.00, "iShares Semiconductor ETF - ETF", "ISIN US4642875235")
    # 港股：HKD 小节，市值原币(HKD) ≠ 美元总值
    lines += ["Emerging Market All Cap Equities (HKD)"]
    lines += block_ccy(1194, 513181.20, 65440.92, "Tencent Holdings Ltd (700 HK)", "Ticker 700 HK Equity")
    tot = 1210412.19 + 1291060.68 + 961140.00 + 65440.92
    lines += ["TOTAL EQUITIES", f"{tot:,.2f}"]
    # 现金汇总（逐币种 + TOTAL CASH）
    lines += [
        "INVESTABLE CASH BY CURRENCY",
        "Currency", "% of Total", "Market Value Nominal Currency", "Market Value USD",
        "USD", "74.08%", "719,962.81", "719,962.81",
        "HKD", "25.92%", "1,975,915.99", "251,969.03",
        "TOTAL CASH", "971,931.84",
    ]

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def citi_pdf():
    return _make_citi_like_pdf()


def test_parse_holdings(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "Integrated Statement for Jun 2026_30_Jun_2026.PDF", broker_hint="citi")
    tickers = {h.ticker for h in stmt.holdings}
    assert {"GOOGL", "META", "700", "US4642875235"} <= tickers, tickers
    g = next(h for h in stmt.holdings if h.ticker == "GOOGL")
    assert g.quantity == 3387 and abs(g.market_value_usd - 1210412.19) < 0.01
    assert g.company == "Alphabet Inc"


def test_etf_isin_anchor_captured(citi_pdf):
    """ETF 用 ISIN 锚（无 Ticker 行）也应抽到——漏 $961,140 那只。"""
    stmt = ingest.ingest_pdf(citi_pdf, "x_30_Jun_2026.PDF", broker_hint="citi")
    etf = next(h for h in stmt.holdings if h.ticker == "US4642875235")
    assert abs(etf.market_value_usd - 961140.00) < 0.01


def test_multicurrency_uses_usd_column(citi_pdf):
    """港股取 Total Value USD（$65,440），非原币市值（HKD 513,181）。"""
    stmt = ingest.ingest_pdf(citi_pdf, "x_30_Jun_2026.PDF", broker_hint="citi")
    hk = next(h for h in stmt.holdings if h.ticker == "700")
    assert hk.nominal_ccy == "HKD"
    assert abs(hk.market_value_usd - 65440.92) < 0.01           # 美元口径
    assert abs(hk.market_value_nominal - 513181.20) < 0.01      # 原币留审计


def test_period_end_from_filename(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "Integrated Statement for Jun 2026_Dan Liu_30_Jun_2026_X.PDF")
    assert stmt.period_end == "2026-06-30"


def test_reconcile_ok(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "x_30_Jun_2026.PDF", broker_hint="citi")
    assert stmt.recon.status == "ok"          # 逐只合计 == TOTAL EQUITIES
    assert stmt.recon.holdings_count == 4
    assert abs(stmt.recon.delta_usd) < 1.0


def test_cash_extracted(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "x_30_Jun_2026.PDF", broker_hint="citi")
    by = {c.currency: c for c in stmt.cash_balances}
    assert abs(by["USD"].market_value_usd - 719962.81) < 0.01
    assert abs(by["HKD"].market_value_usd - 251969.03) < 0.01
    assert abs(stmt.total_cash_usd - 971931.84) < 0.01


def test_content_hash_stable(citi_pdf):
    a = ingest.ingest_pdf(citi_pdf, "x.PDF", broker_hint="citi").content_hash
    b = ingest.ingest_pdf(citi_pdf, "x.PDF", broker_hint="citi").content_hash
    assert a == b and len(a) == 64


def test_detect_broker_and_dispatch(citi_pdf):
    pages = ingest._extract_pages(citi_pdf)
    assert ingest.detect_broker(pages, filename="Integrated Statement for Jun 2026.pdf") == "citi"
    assert ingest.ingest_pdf(citi_pdf, "Integrated Statement for Jun 2026.pdf", broker_hint="citi").broker == "citi"


def test_unsupported_broker_rejected():
    # 一个非花旗的最小 PDF（无 Citi 关键字）→ unsupported_broker
    import fitz
    doc = fitz.open(); pg = doc.new_page(); pg.insert_text((36, 40), "Generic Broker Statement")
    raw = doc.tobytes()
    with pytest.raises(ValueError, match="unsupported_broker"):
        ingest.ingest_pdf(raw, "generic.pdf")


def test_ingest_and_store_encrypts_and_dedups(citi_pdf, tmp_path, monkeypatch):
    # 把 AuthStore 指到临时库
    from bottleneck_hunter.auth import store as store_mod
    monkeypatch.setattr(store_mod, "_DEFAULT_DB", tmp_path / "auth.db")

    r1 = ingest.ingest_and_store(citi_pdf, "x_30_Jun_2026.PDF", user_id="u1")
    assert r1["duplicate"] is False and r1["status"] == "parsed_ok"
    # 幂等：同文件再传 → duplicate
    r2 = ingest.ingest_and_store(citi_pdf, "x_30_Jun_2026.PDF", user_id="u1")
    assert r2["duplicate"] is True and r2["doc_id"] == r1["doc_id"]

    # 明文不落库：密文列查不到公司名
    s = store_mod.AuthStore(tmp_path / "auth.db")
    raw = s.find_financial_doc_by_hash("u1", ingest.ingest_pdf(citi_pdf, "x", broker_hint="citi").content_hash)
    assert "Alphabet" not in raw["parsed_json_encrypted"]
    # 解密可取回
    d = s.get_financial_doc("u1", r1["doc_id"], decrypt_parsed=True)
    assert "Alphabet" in d["parsed_json"]
    # recon_flags 只有 flag，无金额
    import json
    flags = json.loads(raw["recon_flags_json"])
    assert flags["equities_recon"] == "ok"
    assert not any(isinstance(v, float) and v > 1000 for v in flags.values())  # 无金额
