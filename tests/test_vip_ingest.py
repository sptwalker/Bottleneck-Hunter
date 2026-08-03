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


def _make_citi_position_report_pdf() -> bytes:
    import fitz

    lines = [
        "花旗私人银行",
        "报告货币: CNY",
        "资产级别 -  全部持仓",
        "描述",
        "账⼾号码",
        "帐⼾描述",
        "帐⼾代号",
        "当前值",
        "与前⼀天相⽐的变化",
        "与前⼀天相⽐的变化率",
        "名义单位",
        "市价",
        "资产级别",
        "平均或单位成本",
        "总成本基准",
        "未实现盈/(亏)",
        "未实现盈/(亏)%",

        "Call Deposit (IB) USD",
        "0/XXX468/002",
        "7/XXX468/028",
        "Investment Advisory Portfolio",
        "-",
        "CNY 28,530,606.13",
        "$4,214,114.12",
        "CNY 0.00",
        "$0.00",
        "0.00%",
        "4,214,103.410",
        "-",
        "现⾦/投资现⾦",
        "-",
        "-",
        "-",
        "-",

        "Alphabet Inc",
        "Ticker/ISIN",
        "GOOGL/US02079K3059",
        "7/XXX468/028",
        "Investment Advisory Portfolio",
        "-",
        "CNY 7,284,897.53",
        "$1,076,016.03",
        "CNY (559,512.42)",
        "$(82,642.80)",
        "(7.13)%",
        "3,387.000",
        "## $317.69",
        "股票",
        "$185.19",
        "CNY 4,246,578.04",
        "$627,240.95",
        "CNY 3,038,319.49",
        "$448,775.08",
        "71.55%",

        "Tencent Holdings Ltd (700 HK)",
        "Ticker/ISIN",
        "700/KYG875721634",
        "7/XXX468/028",
        "Investment Advisory Portfolio",
        "-",
        "CNY 459,020.68",
        "HKD 531,568.80",
        "CNY 0.00",
        "HKD 0.00",
        "0.00%",
        "1,194.000",
        "## HKD 445.20",
        "股票",
        "HKD 317.35",
        "CNY 327,204.52",
        "HKD 378,919.12",
        "CNY 131,816.16",
        "HKD 152,649.68",
        "40.29%",

        "仓盘",
        "⻚⾯打印在 24 Jul 2026 8:58 AM (UTC+08:00) | 为以下客⼾准备: xxxxxxxxxx",
    ]
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def citi_pdf():
    return _make_citi_like_pdf()


@pytest.fixture
def citi_position_pdf():
    return _make_citi_position_report_pdf()


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


def test_parse_citi_position_report(citi_position_pdf):
    stmt = ingest.ingest_pdf(citi_position_pdf, "全部-仓盘_24_Jul_2026_08_58_40.pdf", broker_hint="citi")
    assert stmt.period_end == "2026-07-24"
    assert stmt.recon.status == "no_statement_total"
    assert {h.ticker for h in stmt.holdings} >= {"GOOGL", "700"}
    googl = next(h for h in stmt.holdings if h.ticker == "GOOGL")
    assert googl.quantity == 3387.0
    assert abs(googl.market_value_usd - 1076016.03) < 0.01
    tencent = next(h for h in stmt.holdings if h.ticker == "700")
    assert tencent.nominal_ccy == "HKD"
    assert abs(tencent.market_value_nominal - 531568.80) < 0.01
    assert stmt.total_cash_usd > 4000000


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


def test_classify_statement_content_fallbacks(citi_position_pdf, monkeypatch):
    from bottleneck_hunter.llm_clients.factory import MissingUserKeyError

    pages = ingest._extract_pages(citi_position_pdf)

    def _run(mode: str):
        if mode == "missing_key":
            monkeypatch.setattr(
                "bottleneck_hunter.llm_clients.factory.get_models_for_role",
                lambda *a, **k: (_ for _ in ()).throw(MissingUserKeyError("anthropic")),
            )
        elif mode == "timeout":
            class _TimeoutLLM:
                def invoke(self, _messages):
                    raise TimeoutError("slow")

            monkeypatch.setattr(
                "bottleneck_hunter.llm_clients.factory.get_models_for_role",
                lambda *a, **k: [(_TimeoutLLM(), "anthropic", "claude")],
            )
        else:
            class _BadLLM:
                def invoke(self, _messages):
                    return type("R", (), {"content": "{}"})()

            monkeypatch.setattr(
                "bottleneck_hunter.llm_clients.factory.get_models_for_role",
                lambda *a, **k: [(_BadLLM(), "anthropic", "claude")],
            )
            monkeypatch.setattr(
                "bottleneck_hunter.chain.json_utils.extract_json_object",
                lambda _text: {"doc_type": "bad", "broker": "???", "confidence": "oops", "reason_code": "bad"},
            )
        return ingest._classify_statement_content(pages, "Integrated Statement for Jun 2026.pdf", user_id="u1")

    for mode in ("missing_key", "timeout", "invalid_json"):
        result = _run(mode)
        assert result["source"] == "heuristic"
        assert result["broker"] == "citi"
        assert result["doc_type"] == "position_report"



def test_merge_classification_citi_deterministic_not_overridden_by_llm():
    """花旗交易/持仓导出的确定性启发式(content_match)不容 LLM 覆盖:LLM 把 交易_*.pdf 误判成
    monthly_statement 时,仍须保 trade_confirm——否则走持仓分支抽空 → unsupported_non_statement:citi。
    但启发式 ambiguous(月结兜底)时应放行 LLM 细分。"""
    h_trade = {"broker": "citi", "doc_type": "trade_confirm", "reason_code": "content_match"}
    llm_wrong = {"broker": "citi", "doc_type": "monthly_statement", "reason_code": "ambiguous"}
    assert ingest._merge_classification(h_trade, llm_wrong)["doc_type"] == "trade_confirm"

    h_ambiguous = {"broker": "citi", "doc_type": "monthly_statement", "reason_code": "ambiguous"}
    llm_refine = {"broker": "citi", "doc_type": "trade_confirm", "reason_code": "content_match"}
    assert ingest._merge_classification(h_ambiguous, llm_refine)["doc_type"] == "trade_confirm"


def test_ingest_and_store_llm_position_report_overrides_filename(citi_position_pdf, tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as store_mod

    monkeypatch.setattr(store_mod, "_DEFAULT_DB", tmp_path / "auth.db")

    class _FakeLLM:
        def invoke(self, _messages):
            return type("R", (), {"content": (
                '{"doc_type":"position_report","broker":"citi",'
                '"confidence":0.97,"reason_code":"content_match"}'
            )})()

    monkeypatch.setattr(
        "bottleneck_hunter.llm_clients.factory.get_models_for_role",
        lambda *a, **k: [(_FakeLLM(), "anthropic", "claude")],
    )

    result = ingest.ingest_and_store(citi_position_pdf, "Integrated Statement for Jun 2026.pdf", user_id="u1")
    assert result["duplicate"] is False
    assert result["doc_type"] == "position_report"

    doc = store_mod.AuthStore(tmp_path / "auth.db").get_financial_doc("u1", result["doc_id"])
    assert doc and doc["doc_type"] == "position_report"



def test_ingest_and_store_llm_trade_confirm_routes_with_hint(citi_pdf, tmp_path, monkeypatch):
    import hashlib

    from bottleneck_hunter.auth import store as store_mod

    monkeypatch.setattr(store_mod, "_DEFAULT_DB", tmp_path / "auth.db")

    class _FakeLLM:
        def invoke(self, _messages):
            return type("R", (), {"content": (
                '{"doc_type":"trade_confirm","broker":"citi",'
                '"confidence":0.96,"reason_code":"content_match"}'
            )})()

    monkeypatch.setattr(
        "bottleneck_hunter.llm_clients.factory.get_models_for_role",
        lambda *a, **k: [(_FakeLLM(), "anthropic", "claude")],
    )

    seen = {}

    def _fake_ingest_pdf(pdf_bytes, filename="", broker_hint="", doc_type_hint="", pdf_password=""):
        seen["broker_hint"] = broker_hint
        seen["doc_type_hint"] = doc_type_hint
        return ingest.BrokerStatement(
            broker="citi",
            period_end="2026-07-24",
            content_hash=hashlib.sha256(pdf_bytes).hexdigest(),
            holdings=[],
            cash_balances=[],
            total_cash_usd=0.0,
            transactions=[ingest.StatementTransaction(
                ticker="AAPL", txn_type="buy", trade_date="2026-07-24",
                quantity=10, price=150.0, gross_amount=-1500.0, net_amount=-1500.0,
                currency="USD", external_id="tc-1", account_ref="A1",
            )],
            recon=ingest.ReconResult(
                holdings_count=0, holdings_total_usd=0.0,
                statement_equities_total_usd=None, delta_usd=None,
                status="no_statement_total",
            ),
        )

    monkeypatch.setattr(ingest, "ingest_pdf", _fake_ingest_pdf)

    result = ingest.ingest_and_store(citi_pdf, "statement.pdf", user_id="u1")
    assert result["doc_type"] == "trade_confirm"
    assert seen["broker_hint"] == "citi"
    assert seen["doc_type_hint"] == "trade_confirm"

    doc = store_mod.AuthStore(tmp_path / "auth.db").get_financial_doc("u1", result["doc_id"])
    assert doc and doc["doc_type"] == "trade_confirm"


# ── 野村 0 持仓事故三处修复回归 ──────────────────────────────────────────

def _mk_stmt(broker="nomura", holdings=(), cash=(), txns=(), summary=None):
    return ingest.BrokerStatement(
        broker=broker, content_hash="x" * 64, period_end="",
        holdings=list(holdings), cash_balances=list(cash), transactions=list(txns),
        account_summary=summary or {},
        recon=ingest.ReconResult(holdings_count=len(holdings), holdings_total_usd=0.0,
                                 statement_equities_total_usd=None, delta_usd=None,
                                 status="no_statement_total"),
    )


def test_detect_broker_nomura_not_stolen_by_generic_account_number():
    """含 'Account Number' 的野村单必须判野村,不被泛化规则截胡成花旗(Statement_260423 事故)。"""
    assert ingest.detect_broker(["Nomura Singapore Limited\nPortfolio Statement\nAccount Number: 1"]) == "nomura"
    assert ingest.detect_broker(["CitiBank\nAccount Number: 9"]) == "citi"         # 花旗品牌优先
    assert ingest.detect_broker(["X Broker\nAccount Number: 2"]) == "citi"          # 无品牌→泛化兜底花旗


def test_statement_is_empty_predicate():
    h = ingest.EquityHolding(ticker="AAA", company="AAA Inc", quantity=1,
                             nominal_ccy="USD", market_value_nominal=10.0, market_value_usd=10.0)
    assert ingest._statement_is_empty(_mk_stmt(holdings=[h])) is False       # 有持仓
    assert ingest._statement_is_empty(_mk_stmt(summary={"net_asset_value_usd": 100})) is False  # 有正锚点
    assert ingest._statement_is_empty(_mk_stmt(summary={"net_asset_value_usd": -8_760_666})) is True  # 负 NAV 不算锚点
    assert ingest._statement_is_empty(_mk_stmt()) is True                    # 全空(irf/披露)


def test_ingest_and_store_rejects_empty_monthly_statement(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as store_mod
    monkeypatch.setattr(store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    monkeypatch.setattr(ingest, "_extract_pages", lambda *a, **k: ["Nomura Singapore Limited"])
    monkeypatch.setattr(ingest, "_classify_statement_content",
                        lambda *a, **k: {"broker": "nomura", "doc_type": "monthly_statement"})
    monkeypatch.setattr(ingest, "ingest_pdf", lambda *a, **k: _mk_stmt())      # 抽成 0 持仓
    with pytest.raises(ValueError, match="unsupported_non_statement"):
        ingest.ingest_and_store(b"%PDF-fake", "disclosure.pdf", user_id="u1")


# ── 花旗「交易活动报告」竖排解析回归（不依赖 PDF 字体，直接喂抽取后的行）──────

def test_parse_citi_transactions_real_layout():
    """真实版式：账户号锚点 + 8 列尾（含独立「交易货币」列）、证券描述跨行、
    康熙部首码位（入=U+2F0A）需 NFKC 规整、会计式括号可落在币种符号后（€(...)）。
    这些正是把整份 18 页交易单抽成脏数据的四个坑，任一回退此测即红。"""
    page = "\n".join([
        # ① 证券买入：种类用康熙部首「已购⼊证券」、描述跨两行、CNY/USD 均括号负数
        "21 Jul 2026",
        "MICRON TECHNOLOG",
        "ISIN US5951121038",
        "7/XXX468/028", "-", "Investment Advisory Portfolio",
        "已购⼊证券", "USD", "CNY (292,090.70)", "USD (43,165.90)", "-", "US5951121038",
        # ② 保证金贷款支取：EUR 括号落在币种符号后 €(...)
        "23 Jul 2026",
        "MARGIN DEMAND LOAN",
        "7/XXX468/028", "-", "Investment Advisory Portfolio",
        "贷款付款", "EUR", "CNY (1,294,974.49)", "€(167,546.74)", "-", "-",
        # ③ 港币股息：非美元、正数、ISIN 有值
        "01 Jun 2026",
        "TENCENT HOLDINGS",
        "ISIN KYG875721634",
        "7/XXX468/028", "-", "Investment Advisory Portfolio",
        "股息", "HKD", "CNY 5,821.94", "HKD 6,328.20", "-", "KYG875721634",
        "⻚⾯打印在 24 Jul 2026 8:59 AM (UTC+08:00)",
    ])
    txns = ingest._parse_citi_transactions([page])
    assert len(txns) == 3, [t.company for t in txns]
    by = {t.company: t for t in txns}

    mic = by["MICRON TECHNOLOG"]                       # 跨行描述已剥掉尾部 ISIN
    assert mic.txn_type == "buy" and mic.currency == "USD"
    assert abs(mic.gross_amount - (-43165.90)) < 0.01
    assert mic.isin == "US5951121038"

    loan = by["MARGIN DEMAND LOAN"]                    # €(...) → 负数
    assert loan.txn_type == "withdrawal" and loan.currency == "EUR"
    assert abs(loan.gross_amount - (-167546.74)) < 0.01
    assert loan.isin == ""

    ten = by["TENCENT HOLDINGS"]
    assert ten.txn_type == "dividend" and ten.currency == "HKD"
    assert abs(ten.gross_amount - 6328.20) < 0.01
    assert ten.isin == "KYG875721634"
