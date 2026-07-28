"""VIP API 端点：门禁(require_vip) + 上传→解析→物化 + 报告生成，走 TestClient。"""
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _citi_pdf() -> bytes:
    import fitz
    def blk(qty, mv, company, anchor):
        return [f"{qty:,.0f}", "185.19", "627,240.95", "357.37", f"{mv:,.2f}",
                "583,171.24", f"{mv:,.2f}", company, "30JUN26", "3.35", anchor]
    lines = ["INVESTMENT POSITIONS", "EQUITIES 60.86% SORTED BY NOM CCY",
             "Developed Large Cap Equities (USD)"]
    lines += blk(100, 200000.0, "Alphabet Inc", "Ticker GOOGL UW Equity")
    lines += blk(50, 80000.0, "Microsoft Corp", "Ticker MSFT UW Equity")
    lines += ["TOTAL EQUITIES", f"{280000.0:,.2f}"]
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


def _nomura_deriv_pdf() -> bytes:
    import fitz
    lines = [
        "International Wealth Management",
        "12 Month USD Daily Accumulator",
        "BE.N, 62.74% Strike Price, 103.00% Knock-out",
        "Summary of final terms and conditions as of 7 July 2026",
        "Trade Date", "7 July 2026",
        "Final Accumulation Date", "6 July 2027",
        "Settlement Currency", "USD",
        "Underlying Share", "BLOOM ENERGY CORP- A (BE UN Equity)",
        "Forward Price", "USD 169.8030 (62.74% of Spot price , rounded to 4 decimal places)",
        "Knock-out Price", "USD 278.7650 (103.00% of Spot price , rounded to 4 decimal places)",
        "Maximum Total Shares", "1,500 (Shares per Day x Maximum Accumulation Days x Gearing Ratio)",
        "Shares per Day", "3",
        "Maximum Accumulation Days", "250",
        "Gearing Ratio", "2",
    ]
    doc = fitz.open(); pg = doc.new_page(); pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


def _citi_trade_export_pdf() -> bytes:
    import fitz
    lines = [
        "Transaction Description", "Account Number", "Account Number 2", "Account Description", "Transaction Type", "Currency", "Base Amount", "Transaction Amount", "CUSIP", "ISIN",
        "24 Jul 2026", "Alphabet Inc Dividend", "123/XXX456/7", "-", "Global account", "DIVIDEND", "CNY 717.50", "USD 100.00", "-", "US02079K3059",
        "23 Jul 2026", "Tencent Fee", "123/XXX456/7", "-", "Global account", "FEE", "CNY 91.75", "HKD 100.00", "-", "HK0700000000",
        "22 Jul 2026", "Cash Transfer", "123/XXX456/7", "-", "Global account", "DEPOSIT", "CNY 3587.50", "USD 500.00", "-", "-",
    ]
    doc = fitz.open(); pg = doc.new_page()
    pg.insert_text((36, 40), "\n".join(lines), fontsize=8)
    return doc.tobytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    # auth.db + watchlist.db 指到临时目录
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    from bottleneck_hunter.watchlist.store import WatchlistStore
    wl = WatchlistStore(tmp_path / "wl.db")

    from bottleneck_hunter.web import vip_api
    vip_api.set_store(wl)

    # 构造 app：只挂 vip_router + 注入 request.state.user（跳过真实 JWT 中间件）
    app = FastAPI()
    _user = {"holder": None}

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = _user["holder"]
        return await call_next(request)

    app.include_router(vip_api.router, prefix="/api/vip")
    c = TestClient(app)
    c._set_user = lambda u: _user.__setitem__("holder", u)
    return c


def test_non_vip_forbidden(client):
    client._set_user({"sub": "u1", "role": "user"})   # 非 VIP
    r = client.get("/api/vip/statements")
    assert r.status_code == 403


def test_account_management_endpoints(client):
    client._set_user({"sub": "admin0", "role": "admin"})

    listed = client.get("/api/vip/accounts?market=us_stock")
    assert listed.status_code == 200, listed.text
    data = listed.json()
    assert data["accounts"] == []
    assert data["default_account"] is None

    created = client.post(
        "/api/vip/accounts?market=us_stock",
        json={
            "account_ref": "CITI-1",
            "display_name": "花旗账户",
            "institution_name": "Citibank",
            "account_kind": "broker",
        },
    )
    assert created.status_code == 200, created.text
    account = created.json()["account"]
    assert account["account_ref"] == "CITI-1"
    assert account["display_name"] == "花旗账户"

    listed2 = client.get("/api/vip/accounts?market=us_stock")
    rows = listed2.json()["accounts"]
    refs = {row["account_ref"] for row in rows}
    assert refs == {"CITI-1"}

    patched = client.patch(
        "/api/vip/accounts/CITI-1?market=us_stock",
        json={"display_name": "花旗环球", "institution_name": "Citi", "account_kind": "bank", "is_default": True},
    )
    assert patched.status_code == 200, patched.text
    acc = patched.json()["account"]
    assert acc["display_name"] == "花旗环球" and acc["account_kind"] == "bank" and acc["is_default"]

    # 旧兼容 query PATCH 已移除
    patched_default = client.patch(
        "/api/vip/accounts?market=us_stock&account_ref=",
        json={"display_name": "默认组合", "institution_name": "Family Office", "account_kind": "bank"},
    )
    assert patched_default.status_code == 405

    # 默认账户不可删
    del_default = client.delete("/api/vip/accounts/CITI-1?market=us_stock")
    assert del_default.status_code == 400

    # 新建一个空账户 → 可删
    client.post("/api/vip/accounts?market=us_stock",
                json={"account_ref": "TMP-1", "display_name": "临时"})
    reorder = client.patch(
        "/api/vip/accounts/order?market=us_stock",
        json={"account_refs": ["TMP-1", "CITI-1"]},
    )
    assert reorder.status_code == 200, reorder.text
    assert [row["account_ref"] for row in reorder.json()["accounts"]] == ["TMP-1", "CITI-1"]

    deleted = client.delete("/api/vip/accounts/TMP-1?market=us_stock")
    assert deleted.status_code == 200, deleted.text
    refs_after = {r["account_ref"] for r in client.get("/api/vip/accounts?market=us_stock").json()["accounts"]}
    assert "TMP-1" not in refs_after

    # 有数据账户不可删：导入月结单后 ACC-2 有持仓/导入记录
    client.post("/api/vip/statements/upload?market=us_stock&account_ref=ACC-2",
                files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    del_used = client.delete("/api/vip/accounts/ACC-2?market=us_stock")
    assert del_used.status_code == 400


def test_account_clear_data_then_delete(client, monkeypatch):
    client._set_user({"sub": "admin7", "role": "admin"})
    client.post(
        "/api/vip/import?market=us_stock&account_ref=ACC-9",
        files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")},
    )
    client.post(
        "/api/vip/statements/upload?market=us_stock&account_ref=ACC-9",
        files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")},
    )

    class _FakeLLM:
        async def astream(self, prompt):
            for x in ["组合总权益 $280,000。", "建议继续观察。"]:
                yield type("C", (), {"content": x})()

    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [(_FakeLLM(), "deepseek", "deepseek-chat")])
    resp = client.post('/api/vip/chat', json={"question": "我的组合情况？", "market": "us_stock", "account_ref": "ACC-9"})
    assert resp.status_code == 200

    blocked = client.delete("/api/vip/accounts/ACC-9?market=us_stock")
    assert blocked.status_code == 400

    imports_all = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert len(imports_all) == 1 and imports_all[0]["account_ref"] == "ACC-9"

    cleared = client.post(
        "/api/vip/accounts/clear-data?market=us_stock",
        json={"account_ref": "ACC-9"},
    )
    assert cleared.status_code == 200, cleared.text
    counts = cleared.json()["reference_counts"]
    assert counts["vip_imports"] == 1
    assert counts["sim_positions"] == 2
    assert counts["chat_sessions"] == 1
    assert counts["chat_messages"] == 2

    imports_after = client.get("/api/vip/imports?market=us_stock&scope=all").json()["imports"]
    assert imports_after == []
    sessions_after = client.get('/api/vip/chat/sessions?account_ref=ACC-9').json()['sessions']
    assert sessions_after == []
    positions_after = client.get('/api/vip/account/positions?market=us_stock&account_ref=ACC-9').json()['positions']
    assert positions_after == []

    deleted = client.delete("/api/vip/accounts/ACC-9?market=us_stock")
    assert deleted.status_code == 200, deleted.text



def test_admin_upload_and_report(client, monkeypatch):
    client._set_user({"sub": "admin1", "role": "admin"})   # admin 直通 VIP
    client.post(
        "/api/vip/accounts?market=us_stock",
        json={"account_ref": "ACC-1", "display_name": "花旗账户", "institution_name": "Citibank", "is_default": True},
    )
    # 上传
    r = client.post("/api/vip/statements/upload?market=us_stock&account_ref=ACC-1",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "parsed_ok" and data["n_positions"] == 2
    assert abs(data["total_equity"] - 280000.0) < 1.0

    # 列文档（无 PII 密文）
    docs = client.get("/api/vip/statements").json()["documents"]
    assert docs and "parsed_json_encrypted" not in docs[0]
    # 按市场过滤真实生效
    docs_us = client.get('/api/vip/statements?market=us_stock').json()['documents']
    docs_cn = client.get('/api/vip/statements?market=a_stock').json()['documents']
    assert len(docs_us) == 1 and docs_cn == []

    # 生成报告（无 AI，避免真实 LLM 调用）
    rr = client.post("/api/vip/reports/generate?with_ai=false&period=2026-06&account_ref=ACC-1")
    assert rr.status_code == 200, rr.text
    rep = rr.json()
    assert "持仓分析报告" in rep["report_md"] and "GOOGL" in rep["report_md"]

    # 上传一个日常衍生品文件 → 列表可见 → 报告自动附衍生品风险摘要
    deriv_pdf = _nomura_deriv_pdf()
    dr = client.post("/api/vip/derivatives/upload?market=us_stock&broker=nomura&account_ref=ACC-1",
                     files={"file": ("oac.pdf", deriv_pdf, "application/pdf")})
    assert dr.status_code == 200, dr.text
    dres = dr.json()
    assert dres["kind"] == "accumulator"
    # 重复上传同一文件应幂等（不重复新增）
    dr2 = client.post("/api/vip/derivatives/upload?market=us_stock&broker=nomura&account_ref=ACC-1",
                      files={"file": ("oac.pdf", deriv_pdf, "application/pdf")})
    assert dr2.status_code == 200
    items = client.get("/api/vip/derivatives?market=us_stock&account_ref=ACC-1").json()["items"]
    assert len(items) == 1 and items[0]["underlying_symbol"] == "BE"
    rr2 = client.post("/api/vip/reports/generate?with_ai=false&period=2026-06&account_ref=ACC-1")
    assert rr2.status_code == 200, rr2.text
    assert "衍生品 / 结构化产品风险摘要" in rr2.json()["report_md"]

    # 列报告
    reps = client.get("/api/vip/reports?account_ref=ACC-1").json()["reports"]
    assert reps and reps[0]["period"] == "2026-06"


def test_total_overview_scope_all(client):
    client._set_user({"sub": "admin5", "role": "admin"})
    client.post(
        "/api/vip/statements/upload?market=us_stock&account_ref=ACC-1",
        files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")},
    )
    client.post(
        "/api/vip/statements/upload?market=us_stock&account_ref=ACC-2",
        files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")},
    )
    ov = client.get("/api/vip/account/overview?market=us_stock&scope=all")
    assert ov.status_code == 200, ov.text
    overview = ov.json()["overview"]
    assert abs(overview["total_equity"] - 560000.0) < 1.0
    assert overview["n_accounts"] == 2
    assert overview["n_holdings"] == 4
    assert overview["total_loan_limit"] is None
    assert {row["account_ref"] for row in overview["holdings"]} == {"ACC-1", "ACC-2"}
    assert {row["account_ref"] for row in overview["accounts"]} == {"ACC-1", "ACC-2"}

    vs_all = client.get("/api/vip/account/value-series?market=us_stock&scope=all")
    assert vs_all.status_code == 200, vs_all.text
    assert len(vs_all.json()["series"]) == 1
    assert abs(vs_all.json()["series"][0]["total_equity"] - 560000.0) < 1.0

    vs_one = client.get("/api/vip/account/value-series?market=us_stock&scope=account&account_ref=ACC-1")
    assert vs_one.status_code == 200, vs_one.text
    assert len(vs_one.json()["series"]) == 1
    assert abs(vs_one.json()["series"][0]["total_equity"] - 280000.0) < 1.0



def test_derivatives_scope_all_and_account(client):
    client._set_user({"sub": "admin6", "role": "admin"})
    deriv_pdf = _nomura_deriv_pdf()
    r1 = client.post("/api/vip/derivatives/upload?market=us_stock&broker=nomura&account_ref=ACC-1",
                     files={"file": ("acc1.pdf", deriv_pdf, "application/pdf")})
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/vip/derivatives/upload?market=us_stock&broker=nomura&account_ref=ACC-2",
                     files={"file": ("acc2.pdf", deriv_pdf, "application/pdf")})
    assert r2.status_code == 200, r2.text

    all_items = client.get("/api/vip/derivatives?market=us_stock&scope=all")
    assert all_items.status_code == 200, all_items.text
    rows = all_items.json()["items"]
    assert len(rows) == 2
    assert {row["account_ref"] for row in rows} == {"ACC-1", "ACC-2"}

    acc1_items = client.get("/api/vip/derivatives?market=us_stock&scope=account&account_ref=ACC-1")
    assert acc1_items.status_code == 200, acc1_items.text
    rows1 = acc1_items.json()["items"]
    assert len(rows1) == 1
    assert rows1[0]["underlying_symbol"] == "BE"

    vs_acc1 = client.get("/api/vip/account/value-series?market=us_stock&scope=account&account_ref=ACC-1")
    assert vs_acc1.status_code == 200, vs_acc1.text
    assert vs_acc1.json()["series"] == []


    client._set_user({"sub": "admin4", "role": "admin"})
    trade_pdf = _citi_trade_export_pdf()
    # 先导入月结单，账户总览锚定 sim_* 当前权益
    r = client.post("/api/vip/statements/upload?market=us_stock&account_ref=ACC-1",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text

    ex = client.post("/api/vip/exports/upload?market=us_stock&account_ref=ACC-1",
                     files={"file": ("交易_24_Jul_2026_08_59_08.pdf", trade_pdf, "application/pdf")})
    assert ex.status_code == 200, ex.text
    data = ex.json()
    assert data["doc_type"] == "trade_confirm"
    assert data["imported_count"] == 3
    assert data["skipped_count"] == 0
    assert data["date_range"] == {"start": "2026-07-22", "end": "2026-07-24"}
    assert data["txn_type_counts"] == {"dividend": 1, "fee": 1, "deposit": 1}
    assert len(data["transactions"]) == 3

    # 重传幂等：不应重复写 transactions
    ex2 = client.post("/api/vip/exports/upload?market=us_stock&account_ref=ACC-1",
                      files={"file": ("交易_24_Jul_2026_08_59_08.pdf", trade_pdf, "application/pdf")})
    assert ex2.status_code == 200, ex2.text
    assert ex2.json()["duplicate"] is True

    ov = client.get("/api/vip/account/overview?market=us_stock&account_ref=ACC-1")
    assert ov.status_code == 200, ov.text
    overview = ov.json()["overview"]
    assert abs(overview["total_equity"] - 280000.0) < 1.0
    assert overview["transaction_count"] == 3
    assert abs(overview["dividend_income"] - 100.0) < 0.01
    assert abs(overview["fee_total"] - 100.0) < 0.01
    assert abs(overview["net_inflow"] - 600.0) < 0.01
    assert abs(overview["net_outflow"] - 100.0) < 0.01
    assert overview["realized_pnl"] is None and overview["realized_pnl_available"] is False

    tx = client.get("/api/vip/account/transactions?market=us_stock&account_ref=ACC-1&txn_type=dividend")
    assert tx.status_code == 200, tx.text
    rows = tx.json()["transactions"]
    assert len(rows) == 1 and rows[0]["txn_type"] == "dividend"

    tx_all = client.get("/api/vip/account/transactions?market=us_stock&account_ref=ACC-1")
    assert len(tx_all.json()["transactions"]) == 3

    # 最近持仓端点：复用月结单物化的 sim_positions
    pos = client.get("/api/vip/account/positions?market=us_stock&account_ref=ACC-1")
    assert pos.status_code == 200, pos.text
    assert len(pos.json()["positions"]) == 2


    client._set_user({"sub": "admin1", "role": "admin"})
    r = client.post("/api/vip/statements/upload",
                    files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400


def test_report_without_holdings(client):
    client._set_user({"sub": "admin2", "role": "admin"})
    client.post(
        "/api/vip/accounts?market=us_stock",
        json={"account_ref": "ACC-EMPTY", "display_name": "空账户", "institution_name": "Citibank", "is_default": True},
    )
    r = client.post("/api/vip/reports/generate?with_ai=false&account_ref=ACC-EMPTY")
    assert r.status_code == 400   # 尚无持仓


def test_import_returns_json_error_when_dispatch_raises(client, monkeypatch):
    client._set_user({"sub": "admin8", "role": "admin"})

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("bottleneck_hunter.vip.importer.dispatch_import", _boom)
    r = client.post(
        "/api/vip/import?market=us_stock",
        files={"file": ("x.pdf", b"%PDF-1.4\nboom", "application/pdf")},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "导入解析失败: boom"


def test_chat_session_endpoints(client, monkeypatch):
    client._set_user({"sub": "admin3", "role": "admin"})
    client.post(
        "/api/vip/accounts?market=us_stock",
        json={"account_ref": "ACC-CHAT", "display_name": "聊天账户", "institution_name": "Citibank", "is_default": True},
    )
    # 先导入最小持仓，聊天 facts 才有内容
    r = client.post("/api/vip/statements/upload?market=us_stock&account_ref=ACC-CHAT",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200

    class _FakeLLM:
        async def astream(self, prompt):
            for x in ["组合总权益 $280,000。", "建议继续观察。"]:
                yield type("C", (), {"content": x})()

    monkeypatch.setattr("bottleneck_hunter.llm_clients.factory.get_models_for_role",
                        lambda *a, **k: [(_FakeLLM(), "deepseek", "deepseek-chat")])
    # SSE 聊天
    resp = client.post('/api/vip/chat', json={"question": "我的组合情况？", "market": "us_stock", "account_ref": "ACC-CHAT"})
    assert resp.status_code == 200
    txt = resp.text
    assert 'event: session' in txt and 'event: done' in txt
    # 会话列表
    ss = client.get('/api/vip/chat/sessions').json()['sessions']
    assert ss and ss[0]['status'] == 'active'
    sid = ss[0]['id']
    msgs = client.get(f'/api/vip/chat/sessions/{sid}').json()['messages']
    assert len(msgs) == 2 and msgs[0]['role'] == 'user' and msgs[1]['role'] == 'assistant'