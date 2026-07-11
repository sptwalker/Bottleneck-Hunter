"""桌面借道 egress 自检 —— transport 路由决策是核心逻辑，必须有覆盖。

不起真实 WS/服务器：用 httpx.MockTransport 当直连、假 RelayConnection 当桌面。
"""

from __future__ import annotations

import httpx
import pytest

from bottleneck_hunter.web.egress_relay import RelayTransport, is_borrow_domain, registry


class _FakeConn:
    """假桌面：fetch 恒返回带标记的响应，标明"这是借道来的"。"""

    def __init__(self, reachable=None):
        self.reachable = set(reachable or [])
        self.calls = []

    def can_reach(self, host):
        return not self.reachable or host in self.reachable

    async def fetch(self, method, url, headers, body):
        self.calls.append(url)
        return httpx.Response(200, content=b"VIA_RELAY")


def _mock_direct(body=b"VIA_DIRECT", raise_exc=None):
    def handler(request):
        if raise_exc:
            raise raise_exc
        return httpx.Response(200, content=body)
    return httpx.MockTransport(handler)


def test_is_borrow_domain():
    assert is_borrow_domain("news.google.com")
    assert is_borrow_domain("www.sec.gov")     # 后缀匹配
    assert is_borrow_domain("data.sec.gov")
    assert not is_borrow_domain("finance.yahoo.com")
    assert not is_borrow_domain("192.168.1.1")


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._relays.clear()
    yield
    registry._relays.clear()


async def test_borrow_domain_routes_via_relay():
    conn = _FakeConn()
    registry.register(conn)
    t = RelayTransport(fallback=_mock_direct())
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://www.sec.gov/files/x.json")
    assert r.content == b"VIA_RELAY"
    assert conn.calls == ["https://www.sec.gov/files/x.json"]


async def test_borrow_domain_falls_back_when_no_relay():
    # 无 relay 连接：白名单域名也直连（零回归）
    t = RelayTransport(fallback=_mock_direct())
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://news.google.com/rss")
    assert r.content == b"VIA_DIRECT"


async def test_non_borrow_domain_goes_direct():
    conn = _FakeConn()
    registry.register(conn)
    t = RelayTransport(fallback=_mock_direct())
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://finance.yahoo.com/quote/AAPL")
    assert r.content == b"VIA_DIRECT"
    assert conn.calls == []  # 非白名单不借道


async def test_non_borrow_failure_falls_back_to_relay():
    # 非白名单直连网络失败 → 兜底借道
    conn = _FakeConn()
    registry.register(conn)
    t = RelayTransport(fallback=_mock_direct(raise_exc=httpx.ConnectError("blocked")))
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://finance.yahoo.com/quote/AAPL")
    assert r.content == b"VIA_RELAY"
    assert conn.calls == ["https://finance.yahoo.com/quote/AAPL"]


async def test_borrow_relay_error_falls_back_direct():
    # 借道抛错 → 回退直连，不整个失败
    class _BadConn(_FakeConn):
        async def fetch(self, *a, **k):
            raise RuntimeError("relay down")
    registry.register(_BadConn())
    t = RelayTransport(fallback=_mock_direct())
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://www.sec.gov/x")
    assert r.content == b"VIA_DIRECT"


# ── WS 线协议两端（帧/base64/id 关联/头剥离）──────────────────────────
class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def send(self, raw):  # 客户端侧用 send(str)
        import json
        self.sent.append(json.loads(raw))


async def test_relay_connection_fetch_roundtrip():
    import asyncio
    import base64
    from bottleneck_hunter.web.egress_relay import RelayConnection
    ws = _FakeWS()
    conn = RelayConnection(ws, reachable=[])
    task = asyncio.create_task(conn.fetch("GET", "https://www.sec.gov/a", {"Host": "x", "User-Agent": "UA"}, b""))
    await asyncio.sleep(0)  # 让 fetch 把 fetch 帧发出去
    sent = ws.sent[0]
    assert sent["type"] == "fetch" and sent["url"] == "https://www.sec.gov/a"
    assert "host" not in {k.lower() for k in sent["headers"]}  # host 头被剥
    # 模拟桌面回帧：带 content-encoding（应被服务端剥掉，避免二次解码）
    conn.resolve({"id": sent["id"], "status": 200,
                  "headers": {"content-type": "application/json", "content-encoding": "gzip"},
                  "body_b64": base64.b64encode(b'{"ok":1}').decode()})
    resp = await task
    assert resp.status_code == 200 and resp.content == b'{"ok":1}'
    assert "content-encoding" not in {k.lower() for k in resp.headers}


async def test_client_handle_fetch_rejects_non_allowlist():
    from bottleneck_hunter.relay_client import _handle_fetch
    ws = _FakeWS()
    client = httpx.AsyncClient(transport=_mock_direct(b"SECRET"))
    await _handle_fetch(client, ws, {"id": "1", "url": "http://192.168.0.1/admin", "method": "GET"})
    await client.aclose()
    assert ws.sent[0]["error"] and "白名单" in ws.sent[0]["error"]
    assert "body_b64" not in ws.sent[0]  # 未取数


async def test_client_handle_fetch_success():
    import base64
    from bottleneck_hunter.relay_client import _handle_fetch
    ws = _FakeWS()
    client = httpx.AsyncClient(transport=_mock_direct(b"RSSDATA"))
    await _handle_fetch(client, ws, {"id": "9", "url": "https://news.google.com/rss", "method": "GET"})
    await client.aclose()
    out = ws.sent[0]
    assert out["id"] == "9" and out["status"] == 200
    assert base64.b64decode(out["body_b64"]) == b"RSSDATA"


# ── 真实 WS 端点：鉴权边界 + hello 注册（TestClient 打真握手，不碰 DB）──
def _bare_app():
    from fastapi import FastAPI
    from bottleneck_hunter.web.egress_api import router
    app = FastAPI()
    app.include_router(router, prefix="/api/egress")
    return app


def test_ws_rejects_non_admin():
    from starlette.testclient import TestClient
    from bottleneck_hunter.auth.jwt_utils import create_token
    tok = create_token("u1", "bob", role="user")
    client = TestClient(_bare_app())
    import pytest as _pt
    with _pt.raises(Exception):  # 非 admin → 端点 close(4403)，握手失败
        with client.websocket_connect(f"/api/egress/relay?token={tok}") as ws:
            ws.send_json({"type": "hello", "reachable": []})
            ws.receive_json()
    assert registry.status()["connected"] is False


def test_ws_admin_registers_and_reports():
    import time
    from starlette.testclient import TestClient
    from bottleneck_hunter.auth.jwt_utils import create_token
    tok = create_token("admin1", "root", role="admin")
    client = TestClient(_bare_app())
    with client.websocket_connect(f"/api/egress/relay?token={tok}") as ws:
        ws.send_json({"type": "hello", "reachable": ["www.sec.gov"]})
        for _ in range(50):  # 给端点处理 hello 的时间
            if registry.status()["connected"]:
                break
            time.sleep(0.01)
        st = registry.status()
        assert st["connected"] and "www.sec.gov" in st["reachable"]
    for _ in range(50):  # 断开后应注销
        if not registry.status()["connected"]:
            break
        time.sleep(0.01)
    assert registry.status()["connected"] is False
