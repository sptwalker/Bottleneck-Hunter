"""桌面借道 API — 挂载在 /api/egress。

- WS  /relay   admin 桌面小助手拨入注册为出站中继（token 走 query 或 cookie）。
- GET  /status admin 查看借道连接状态（前端状态条用）。

安全：仅 role==admin 可接入；实际取数域名由 egress_relay 白名单双向约束（防 SSRF）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from bottleneck_hunter.auth.dependencies import get_current_user
from bottleneck_hunter.auth.jwt_utils import get_cookie_name, verify_token
from bottleneck_hunter.web.egress_relay import RelayConnection, registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["egress"])


def _require_admin(user: dict = Depends(get_current_user)) -> dict:
    from fastapi import HTTPException
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可用")
    return user


@router.get("/status")
async def egress_status(user: dict = Depends(_require_admin)):
    return registry.status()


@router.websocket("/relay")
async def egress_relay(websocket: WebSocket):
    token = websocket.query_params.get("token") or websocket.cookies.get(get_cookie_name())
    payload = verify_token(token) if token else None
    if not payload or payload.get("role") != "admin":
        await websocket.close(code=4403)
        return

    await websocket.accept()
    try:
        hello = await websocket.receive_json()
    except Exception:  # noqa: BLE001
        await websocket.close(code=4400)
        return
    conn = RelayConnection(websocket, hello.get("reachable", []) if isinstance(hello, dict) else [])
    registry.register(conn)
    try:
        while True:
            msg = await websocket.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "result":
                conn.resolve(msg)
            # 其余帧（ping 等）忽略；连接存活由 WS 协议层 ping/pong 维持
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info("egress relay 收帧异常: %s", e)
    finally:
        registry.unregister(conn)
