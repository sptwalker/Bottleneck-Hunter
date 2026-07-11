"""桌面借道小助手（客户端）— 在 admin 的桌面 PC 常驻，借本机网络替服务器取新闻/SEC 数据。

流程：账号密码登录服务器拿 token → wss 拨入 /api/egress/relay → 收到「取这个 URL」用本机
httpx 取回原始字节回传。出站域名受 egress_relay 白名单约束（拒绝白名单外 URL，防被指挥乱抓）。

用法：bottleneck-hunter relay --server https://your-server[:port]
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from urllib.parse import urlparse

import httpx
import websockets

from bottleneck_hunter.web.egress_relay import BORROW_HOSTS, BORROW_SUFFIXES, is_borrow_domain

logger = logging.getLogger(__name__)

_PROBE_HOSTS = sorted(BORROW_HOSTS | {"www." + s for s in BORROW_SUFFIXES})
_KEEP_RESP_HEADERS = {"content-type", "date", "last-modified", "etag"}


async def _login(server: str, username: str, password: str) -> str:
    """登录取 bh_token（从 Set-Cookie）。"""
    async with httpx.AsyncClient(base_url=server, timeout=20, follow_redirects=True) as c:
        r = await c.post("/api/auth/login", json={"username": username, "password": password})
        if r.status_code != 200:
            raise RuntimeError(f"登录失败({r.status_code}): {r.text[:200]}")
        token = c.cookies.get("bh_token")
        if not token:
            raise RuntimeError("登录成功但未拿到 token（服务器未下发 cookie？）")
        return token


async def _probe_reachable(client: httpx.AsyncClient) -> list[str]:
    """探测本机能连通哪些借道站（任何响应/状态都算可达）。"""
    async def _one(host: str) -> str | None:
        try:
            await client.get(f"https://{host}/", timeout=6)
            return host
        except Exception:  # noqa: BLE001
            return None
    results = await asyncio.gather(*[_one(h) for h in _PROBE_HOSTS])
    return [h for h in results if h]


async def _handle_fetch(client: httpx.AsyncClient, ws, msg: dict) -> None:
    rid = msg.get("id")
    url = msg.get("url", "")
    host = urlparse(url).hostname or ""
    if not is_borrow_domain(host):
        print(f"  ✗ 拒绝（非白名单）: {host}", flush=True)
        await ws.send(json.dumps({"type": "result", "id": rid, "error": f"域名不在借道白名单: {host}"}))
        return
    body = base64.b64decode(msg["body_b64"]) if msg.get("body_b64") else None
    print(f"  → 借道取数 {msg.get('method', 'GET')} {url}", flush=True)
    try:
        r = await client.request(msg.get("method", "GET"), url,
                                 headers=msg.get("headers") or {}, content=body)
        headers = {k: v for k, v in r.headers.items() if k.lower() in _KEEP_RESP_HEADERS}
        print(f"    ✓ {r.status_code} · {len(r.content)}B", flush=True)
        await ws.send(json.dumps({
            "type": "result", "id": rid, "status": r.status_code, "headers": headers,
            "body_b64": base64.b64encode(r.content).decode(),
        }))
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ 取数失败: {e}", flush=True)
        await ws.send(json.dumps({"type": "result", "id": rid, "error": str(e)}))


async def _run_once(server: str, token: str) -> None:
    ws_url = server.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    ws_url += f"/api/egress/relay?token={token}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        reachable = await _probe_reachable(client)
        print(f"  可达借道站: {reachable or '（探测失败，仍尝试连接）'}")
        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "hello", "reachable": reachable}))
            print("  ✅ 已连上服务器，借道就绪。服务器需要时会自动借用本机网络。")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("type") == "fetch":
                    asyncio.create_task(_handle_fetch(client, ws, msg))


async def run_relay(server: str, username: str, password: str) -> None:
    """登录 + 常驻（断线自动重连）。"""
    token = await _login(server, username, password)
    print("  登录成功。")
    backoff = 2
    while True:
        try:
            await _run_once(server, token)
            backoff = 2
        except Exception as e:  # noqa: BLE001
            print(f"  连接断开/失败: {e} —— {backoff}s 后重连")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            # token 可能过期，重登
            try:
                token = await _login(server, username, password)
            except Exception as le:  # noqa: BLE001
                print(f"  重新登录失败: {le}")
