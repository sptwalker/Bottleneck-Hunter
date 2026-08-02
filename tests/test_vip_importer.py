"""VIP 通用导入分发器：类型判别 + 路由入库 + 去重/拒绝/无法解读 + 导入历史留痕。"""
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _citi_pdf() -> bytes:
    import fitz
    def blk(qty, mv, company, anchor):
        return [f"{qty:,.0f}", "185.19", "627,240.95", "357.37", f"{mv:,.2f}",
                "583,171.24", f"{mv:,.2f}", company, "30JUN26", "3.35", anchor]
    lines = ["CITIBANK N.A. — INTEGRATED STATEMENT",
             "INVESTMENT POSITIONS", "EQUITIES 60.86% SORTED BY NOM CCY",
             "Developed Large Cap Equities (USD)"]
    lines += blk(100, 200000.0, "Alphabet Inc", "Ticker GOOGL UW Equity")
    lines += blk(50, 80000.0, "Microsoft Corp", "Ticker MSFT UW Equity")
    lines += ["TOTAL EQUITIES", f"{280000.0:,.2f}"]
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


def _plain_pdf(text: str) -> bytes:
    import fitz
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), text, fontsize=10)
    return doc.tobytes()


def _citi_position_pdf() -> bytes:
    import fitz

    lines = [
        "花旗私人银行",
        "报告货币: CNY",
        "资产级别 -  全部持仓",
        "Ticker/ISIN",
        "GOOGL/US02079K3059",
        "Investment Advisory Portfolio",
        "$1,076,016.03",
        "3,387.000",
        "仓盘",
        "⻚⾯打印在 24 Jul 2026 8:58 AM (UTC+08:00)",
    ]
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


def _citi_trade_export_pdf() -> bytes:
    import fitz
    lines = [
        "Transaction Description", "Account Number", "Account Number 2", "Account Description", "Transaction Type", "Currency", "Base Amount", "Transaction Amount", "CUSIP", "ISIN",
        "24 Jul 2026", "Alphabet Inc Dividend", "123/XXX456/7", "-", "Global account", "DIVIDEND", "USD", "CNY 717.50", "USD 100.00", "-", "US02079K3059",
        "23 Jul 2026", "Tencent Fee", "123/XXX456/7", "-", "Global account", "FEE", "HKD", "CNY 91.75", "HKD 100.00", "-", "HK0700000000",
        "22 Jul 2026", "Cash Transfer", "123/XXX456/7", "-", "Global account", "DEPOSIT", "USD", "CNY 3587.50", "USD 500.00", "-", "-",
    ]
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    from bottleneck_hunter.watchlist.store import WatchlistStore
    wl = WatchlistStore(tmp_path / "wl.db")

    from bottleneck_hunter.web import vip_api
    vip_api.set_store(wl)

    app = FastAPI()
    _user = {"holder": None}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = _user["holder"]
        return await call_next(request)

    app.include_router(vip_api.router, prefix="/api/vip")
    c = TestClient(app)

    def _set_user(u):
        _user["holder"] = u
        if u and u.get("sub"):
            vip_api._unlocked_subs.add(u["sub"])   # 注入用户 = 已登录且已解锁的 VIP（真实用户凭密码解锁一次/会话）

    c._set_user = _set_user
    c._set_user({"sub": "admin1", "role": "admin"})
    return c


def test_dispatch_requires_real_account_before_import(client):
    pdf = _citi_pdf()
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("stmt_30_Jun_2026.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "rejected"
    assert data["reason"] == "no_real_accounts"

    imports = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert imports == []



def test_statement_import_materializes_positions_and_value_series(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "ONLY-1", "display_name": "唯一账户", "institution_name": "Citibank"})
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "imported"
    assert data["detected_kind"] == "monthly_statement"
    assert data["resolved_account_ref"] == "ONLY-1"

    pos = client.get("/api/vip/account/positions?market=us_stock&account_ref=ONLY-1")
    assert pos.status_code == 200, pos.text
    assert len(pos.json()["positions"]) == 2

    vs = client.get("/api/vip/account/value-series?market=us_stock&scope=account&account_ref=ONLY-1")
    assert vs.status_code == 200, vs.text
    assert len(vs.json()["series"]) == 1
    assert abs(vs.json()["series"][0]["total_equity"] - 280000.0) < 1.0



def test_imports_scope_all_aggregates_accounts(client):
    client.post("/api/vip/import?market=us_stock&account_ref=ACC-1",
                files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    client.post("/api/vip/import?market=us_stock&account_ref=ACC-2",
                files={"file": ("Integrated Statement for Jun 2026.pdf", _citi_position_pdf(), "application/pdf")})

    scoped = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert len(scoped) == 2
    assert {row["account_ref"] for row in scoped} == {"ACC-1", "ACC-2"}



def test_dispatch_position_report_and_history(client):
    pdf = _citi_position_pdf()
    r = client.post("/api/vip/import?market=us_stock&account_ref=A1",
                    files={"file": ("Integrated Statement for Jun 2026.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "imported"
    assert data["detected_kind"] == "position_report"
    assert "当前持仓导出" in data["summary"]

    imports = client.get("/api/vip/imports?market=us_stock&account_ref=A1").json()["imports"]
    assert len(imports) == 1
    assert imports[0]["detected_kind"] == "position_report"


def test_dispatch_rejects_unknown_without_writing_history_when_no_account(client):
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("random.pdf", _plain_pdf("Hello unrelated document"), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "rejected"
    assert data["reason"] == "no_real_accounts"
    imports = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert imports == []


def test_auto_assigns_single_real_account(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "ONLY-1", "display_name": "唯一账户", "institution_name": "Citibank"})
    pdf = _citi_pdf()
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("stmt_30_Jun_2026.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "imported"
    assert data["resolved_account_ref"] == "ONLY-1"
    assert "自动归户" in data["summary"]

    imports = client.get("/api/vip/imports?market=us_stock&account_ref=ONLY-1").json()["imports"]
    assert len(imports) == 1 and imports[0]["account_ref"] == "ONLY-1"



def test_auto_assigns_statement_account_ref(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "123/XXX456/7", "display_name": "花旗环球", "institution_name": "Citibank"})
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "OTHER-1", "display_name": "其他账户", "institution_name": "BrokerX"})
    pdf = _citi_trade_export_pdf()
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("交易_24_Jul_2026_08_59_08.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "imported"
    assert data["resolved_account_ref"] == "123/XXX456/7"



def test_trade_confirm_import_does_not_create_positions_or_value_series(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "123/XXX456/7", "display_name": "花旗环球", "institution_name": "Citibank"})
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("交易_24_Jul_2026_08_59_08.pdf", _citi_trade_export_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "imported"
    assert data["detected_kind"] == "trade_confirm"
    assert data["resolved_account_ref"] == "123/XXX456/7"

    tx = client.get("/api/vip/account/transactions?market=us_stock&account_ref=123%2FXXX456%2F7")
    assert tx.status_code == 200, tx.text
    assert len(tx.json()["transactions"]) == 3

    pos = client.get("/api/vip/account/positions?market=us_stock&account_ref=123%2FXXX456%2F7")
    assert pos.status_code == 200, pos.text
    assert pos.json()["positions"] == []

    vs = client.get("/api/vip/account/value-series?market=us_stock&scope=account&account_ref=123%2FXXX456%2F7")
    assert vs.status_code == 200, vs.text
    assert vs.json()["series"] == []



def test_returns_confirmation_when_broker_matches_multiple_accounts(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "CITI-1", "display_name": "花旗一号", "institution_name": "Citibank"})
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "CITI-2", "display_name": "花旗二号", "institution_name": "Citibank"})
    r = client.post("/api/vip/import?market=us_stock",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "needs_account_confirmation"
    assert data["reason"] == "account_confirmation_required"
    assert len(data["account_candidates"]) == 2
    refs = {row["account_ref"] for row in data["account_candidates"]}
    assert refs == {"CITI-1", "CITI-2"}

    imports = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert imports == []


def test_value_series_and_missing_require_explicit_or_unique_account(client):
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "ONLY-1", "display_name": "唯一账户", "institution_name": "Citibank"})
    client.post("/api/vip/import?market=us_stock",
                files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    vs = client.get("/api/vip/account/value-series?market=us_stock&account_ref=ONLY-1").json()
    assert len(vs["series"]) == 1                       # 单期 → 单点
    assert vs["series"][0]["as_of_date"] == "2026-06-30"

    missing = client.get("/api/vip/account/missing?market=us_stock&account_ref=ONLY-1").json()["missing"]
    codes = {m["code"] for m in missing}
    assert "value_series" in codes                      # 仅 1 期 → 提示补充
    assert "positions" not in codes                     # 已有持仓


def test_value_series_multi_period_unit(tmp_path):
    """两期不同 as_of_date → 曲线两点 + 一段收益率（直接单测聚合 SQL）。"""
    from bottleneck_hunter.vip import portfolio
    from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
    from bottleneck_hunter.watchlist.store import WatchlistStore

    wl = WatchlistStore(tmp_path / "wl2.db").for_user("u1").for_market("us_stock")
    wl.create_vip_account(account_ref="A1", display_name="账户1")

    def stmt(period, mv):
        return BrokerStatement(
            broker="citi", period_end=period, content_hash=period + str(mv),
            holdings=[EquityHolding(ticker="AAPL", company="Apple", quantity=10, market_value_usd=mv)],
            recon=ReconResult(holdings_count=1, holdings_total_usd=mv,
                              statement_equities_total_usd=mv, delta_usd=0.0, status="ok"))

    portfolio.normalize_statement(wl, stmt("2026-05-31", 1000.0), source_doc_id="d1", account_ref="A1")
    portfolio.normalize_statement(wl, stmt("2026-06-30", 1200.0), source_doc_id="d2", account_ref="A1")

    vs = portfolio.value_series(wl, account_ref="A1")
    assert [s["as_of_date"] for s in vs["series"]] == ["2026-05-31", "2026-06-30"]
    assert vs["series"][1]["total_equity"] == 1200.0
    assert len(vs["returns"]) == 1
    assert vs["returns"][0]["pct"] == 20.0


def test_broker_alias_matches_chinese_institution_name():
    """英文 broker 名应能命中中文机构名（否则中文账户按券商自动归户永远失效）。"""
    from bottleneck_hunter.vip.importer import _match_accounts_by_broker
    accounts = [
        {"account_ref": "NOMURA", "institution_name": "野村"},
        {"account_ref": "CITI-1", "institution_name": "花旗环球"},
    ]
    nomura = _match_accounts_by_broker(accounts, "nomura")
    assert [a["account_ref"] for a in nomura] == ["NOMURA"]
    citi = _match_accounts_by_broker(accounts, "citi")
    assert [a["account_ref"] for a in citi] == ["CITI-1"]
    # 英文机构名仍照常命中，unknown 不匹配任何账户
    assert _match_accounts_by_broker([{"account_ref": "X", "institution_name": "Nomura Singapore"}], "nomura")
    assert _match_accounts_by_broker(accounts, "unknown") == []
