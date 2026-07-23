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
    lines = ["INVESTMENT POSITIONS", "EQUITIES 60.86% SORTED BY NOM CCY",
             "Nominal Ccy", "Quantity", "Description", "Market Value"]
    lines += block(3387, 1210412.19, "Alphabet Inc")
    lines += block(2292, 1291060.68, "Meta Platforms Inc")
    lines += ["TOTAL EQUITIES", f"{1210412.19 + 1291060.68:,.2f}"]

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def citi_pdf():
    return _make_citi_like_pdf()


def test_parse_holdings(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "Integrated Statement for Jun 2026_30_Jun_2026.PDF")
    tickers = {h.ticker for h in stmt.holdings}
    assert tickers == {"GOOGL", "META"}, tickers
    g = next(h for h in stmt.holdings if h.ticker == "GOOGL")
    assert g.quantity == 3387 and abs(g.market_value_usd - 1210412.19) < 0.01
    assert g.company == "Alphabet Inc"


def test_period_end_from_filename(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "Integrated Statement for Jun 2026_Dan Liu_30_Jun_2026_X.PDF")
    assert stmt.period_end == "2026-06-30"


def test_reconcile_ok(citi_pdf):
    stmt = ingest.ingest_pdf(citi_pdf, "x_30_Jun_2026.PDF")
    assert stmt.recon.status == "ok"          # 逐只合计 == TOTAL EQUITIES
    assert stmt.recon.holdings_count == 2
    assert abs(stmt.recon.delta_usd) < 1.0


def test_content_hash_stable(citi_pdf):
    a = ingest.ingest_pdf(citi_pdf, "x.PDF").content_hash
    b = ingest.ingest_pdf(citi_pdf, "x.PDF").content_hash
    assert a == b and len(a) == 64


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
    raw = s.find_financial_doc_by_hash("u1", ingest.ingest_pdf(citi_pdf, "x").content_hash)
    assert "Alphabet" not in raw["parsed_json_encrypted"]
    # 解密可取回
    d = s.get_financial_doc("u1", r1["doc_id"], decrypt_parsed=True)
    assert "Alphabet" in d["parsed_json"]
    # recon_flags 只有 flag，无金额
    import json
    flags = json.loads(raw["recon_flags_json"])
    assert flags["equities_recon"] == "ok"
    assert not any(isinstance(v, float) and v > 1000 for v in flags.values())  # 无金额
