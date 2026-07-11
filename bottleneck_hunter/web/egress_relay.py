"""桌面借道 egress（v1）— 服务器无法直连的新闻/SEC 站，借 admin 桌面小助手取数。

三块：
- BORROW 白名单：哪些域名走借道（服务端与客户端共用一份，避免 SSRF：客户端只允许这些站）。
- RelayRegistry / RelayConnection：管理已连接的桌面小助手，request/response 用 id 关联 future。
- RelayTransport：挂到 `retry.get_http_client()` 的共享 httpx 客户端上——白名单域名优先走 relay，
  非白名单域名直连、直连网络失败时兜底走 relay（用户选的「白名单+失败兜底」策略）。

无 relay 连接时一律回退直连 = 现状，零回归（需求③：能直连就服务器自己取）。
"""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx

logger = logging.getLogger(__name__)

# ── 借道白名单（单一事实源；客户端 import 同一份做出站校验）─────────────
# 只放「我们代码显式发起 requests/httpx 的新闻/SEC/宏观 URL」的域名（scope-lite v1）。
BORROW_HOSTS = {"news.google.com", "api.stlouisfed.org"}
BORROW_SUFFIXES = {"sec.gov"}  # 覆盖 www./data./efts.sec.gov

RELAY_TIMEOUT = 30.0  # 单次借道取数上限（秒）


def is_borrow_domain(host: str) -> bool:
    host = (host or "").lower()
    if host in BORROW_HOSTS:
        return True
    return any(host == s or host.endswith("." + s) for s in BORROW_SUFFIXES)


# ── 中继连接 ──────────────────────────────────────────────────────────
class RelayConnection:
    """一个已连接的桌面小助手。fetch() 由 transport 调用并 await；resolve() 由 WS 收帧循环调用。"""

    def __init__(self, ws, reachable: list[str]):
        self.ws = ws
        self.reachable = {h.lower() for h in (reachable or [])}
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0

    def can_reach(self, host: str) -> bool:
        # 桌面若上报了可达清单则按清单；未上报（空）则默认能取白名单内任何站。
        if not self.reachable:
            return True
        host = (host or "").lower()
        return host in self.reachable or any(host.endswith("." + d) for d in self.reachable)

    async def fetch(self, method: str, url: str, headers: dict, body: bytes) -> httpx.Response:
        self._counter += 1
        rid = str(self._counter)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        # 去掉 host 头，让桌面 httpx 自行设置；body 走 base64。
        hdrs = {k: v for k, v in headers.items() if k.lower() != "host"}
        await self.ws.send_json({
            "type": "fetch", "id": rid, "method": method, "url": url,
            "headers": hdrs, "body_b64": base64.b64encode(body).decode() if body else None,
        })
        try:
            payload = await asyncio.wait_for(fut, timeout=RELAY_TIMEOUT)
        finally:
            self._pending.pop(rid, None)
        if payload.get("error"):
            raise RuntimeError(f"relay 取数失败: {payload['error']}")
        content = base64.b64decode(payload["body_b64"]) if payload.get("body_b64") else b""
        # 桌面回传的是已解码正文；剥掉会导致二次解码/长度不符的头。
        resp_headers = {k: v for k, v in (payload.get("headers") or {}).items()
                        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
        return httpx.Response(status_code=payload.get("status", 502), headers=resp_headers, content=content)

    def resolve(self, payload: dict) -> None:
        fut = self._pending.get(payload.get("id"))
        if fut and not fut.done():
            fut.set_result(payload)

    def fail_all(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(reason))


# ── 注册表（进程内；单 admin 单桌面够用，支持多连接取首个可达）──────────
class RelayRegistry:
    def __init__(self):
        self._relays: list[RelayConnection] = []

    def register(self, conn: RelayConnection) -> None:
        self._relays.append(conn)
        logger.info("egress relay 已连接（当前 %d 个），可达=%s", len(self._relays), conn.reachable or "全部白名单")

    def unregister(self, conn: RelayConnection) -> None:
        conn.fail_all("relay 已断开")
        if conn in self._relays:
            self._relays.remove(conn)
        logger.info("egress relay 已断开（剩 %d 个）", len(self._relays))

    def pick(self, host: str) -> RelayConnection | None:
        for c in self._relays:
            if c.can_reach(host):
                return c
        return None

    def status(self) -> dict:
        reachable = sorted({h for c in self._relays for h in c.reachable})
        return {"connected": bool(self._relays), "count": len(self._relays), "reachable": reachable}


registry = RelayRegistry()


# ── httpx 自定义 transport ────────────────────────────────────────────
class RelayTransport(httpx.AsyncBaseTransport):
    """白名单域名优先借道；非白名单直连、网络失败兜底借道。"""

    def __init__(self, fallback: httpx.AsyncBaseTransport):
        self._fallback = fallback

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if is_borrow_domain(host):
            conn = registry.pick(host)
            if conn is not None:
                logger.info("egress: 🛰 借道取数 → %s", host)
                try:
                    return await self._relay(conn, request)
                except Exception as e:  # noqa: BLE001
                    logger.warning("egress: 借道 %s 失败，回退直连: %s", host, e)
            else:
                logger.info("egress: 白名单 %s 但无可用 relay（reachable 不匹配？），直连", host)
            return await self._fallback.handle_async_request(request)
        # 非白名单：直连优先，网络层失败才兜底借道。
        try:
            return await self._fallback.handle_async_request(request)
        except httpx.TransportError as e:
            conn = registry.pick(host)
            if conn is not None:
                logger.info("直连 %s 失败，兜底借道: %s", host, e)
                try:
                    return await self._relay(conn, request)
                except Exception:  # noqa: BLE001
                    logger.warning("兜底借道 %s 也失败", host)
            raise

    @staticmethod
    async def _relay(conn: RelayConnection, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        return await conn.fetch(request.method, str(request.url), dict(request.headers), body)


def build_relay_transport(fallback: httpx.AsyncBaseTransport | None = None) -> RelayTransport:
    return RelayTransport(fallback or httpx.AsyncHTTPTransport())
