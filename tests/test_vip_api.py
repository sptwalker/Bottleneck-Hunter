"""VIP API 端点：门禁(require_vip) + 上传→解析→物化 + 报告生成，走 TestClient。"""
import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402
from fastapi import FastAPI  # noqa: E402


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


def test_admin_upload_and_report(client, monkeypatch):
    client._set_user({"sub": "admin1", "role": "admin"})   # admin 直通 VIP
    # 上传
    r = client.post("/api/vip/statements/upload?market=us_stock",
                    files={"file": ("stmt_30_Jun_2026.pdf", _citi_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "parsed_ok" and data["n_positions"] == 2
    assert abs(data["total_equity"] - 280000.0) < 1.0

    # 列文档（无 PII 密文）
    docs = client.get("/api/vip/statements").json()["documents"]
    assert docs and "parsed_json_encrypted" not in docs[0]

    # 生成报告（无 AI，避免真实 LLM 调用）
    rr = client.post("/api/vip/reports/generate?with_ai=false&period=2026-06")
    assert rr.status_code == 200, rr.text
    rep = rr.json()
    assert "持仓分析报告" in rep["report_md"] and "GOOGL" in rep["report_md"]

    # 列报告
    reps = client.get("/api/vip/reports").json()["reports"]
    assert reps and reps[0]["period"] == "2026-06"


def test_non_pdf_rejected(client):
    client._set_user({"sub": "admin1", "role": "admin"})
    r = client.post("/api/vip/statements/upload",
                    files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400


def test_report_without_holdings(client):
    client._set_user({"sub": "admin2", "role": "admin"})
    r = client.post("/api/vip/reports/generate?with_ai=false")
    assert r.status_code == 400   # 尚无持仓
