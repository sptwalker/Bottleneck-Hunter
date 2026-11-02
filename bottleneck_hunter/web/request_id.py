"""Request ID 中间件：入站校验 → ContextVar → 响应头回写。

放在最外层（最后注册），确保它覆盖 AuthMiddleware / OpLogMiddleware 的所有日志。
入站头不可信：非法字符或超长一律重新生成，避免日志注入。
"""

from __future__ import annotations

from bottleneck_hunter.web.log_context import reset_request_id, sanitize_request_id, set_request_id

HEADER = "x-request-id"


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope.get("headers") or []:
            if name.decode("latin-1").lower() == HEADER:
                incoming = value.decode("latin-1")
                break

        request_id = sanitize_request_id(incoming)
        token = set_request_id(request_id)

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            reset_request_id(token)
