"""FastAPI application factory for BottleneckHunter web UI."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

_log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from bottleneck_hunter.auth.jwt_utils import get_cookie_name, verify_token
from bottleneck_hunter.auth.migration import run_migration
from bottleneck_hunter.auth.store import AuthStore
from bottleneck_hunter.web.api import router
from bottleneck_hunter.web.auth_api import router as auth_router, set_auth_store, set_wl_store as auth_set_wl_store
from bottleneck_hunter.web.watchlist_api import router as watchlist_router, set_store as wl_set_store, set_auth_store as wl_set_auth_store
from bottleneck_hunter.web.decision_api import router as decision_router, set_store as dc_set_store
from bottleneck_hunter.web.trading_api import router as trading_router, set_store as st_set_store
from bottleneck_hunter.web.user_api import router as user_router, set_auth_store as user_set_auth_store
from bottleneck_hunter.web.admin_api import router as admin_router, set_stores as admin_set_stores
from bottleneck_hunter.web.syslog_api import router as syslog_router, init_broadcaster, shutdown_broadcaster
from bottleneck_hunter.web.custom_provider_api import (
    router as custom_provider_router,
    set_auth_store as cp_set_auth_store,
)
from bottleneck_hunter.web.data_source_api import (
    router as data_source_router,
    set_auth_store as ds_set_auth_store,
)
from bottleneck_hunter.web.data_report_api import (
    router as data_report_router,
    set_stores as data_report_set_stores,
)
from bottleneck_hunter.web.ai_config_api import (
    router as ai_config_router,
    set_store as aic_set_store,
    set_auth_store as aic_set_auth_store,
)
from bottleneck_hunter.web.reverse_api import (
    router as reverse_router,
    set_store as reverse_set_store,
)
from bottleneck_hunter.web.settings_api import (
    router as settings_router,
    set_stores as settings_set_stores,
)
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.scheduler import init_scheduler, shutdown_scheduler
from bottleneck_hunter.watchlist.retry import close_http_client

STATIC_DIR = Path(__file__).parent / "static"

_wl_store = WatchlistStore()
_auth_store = AuthStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 认证初始化
    admin = _auth_store.ensure_default_admin()
    set_auth_store(_auth_store)
    auth_set_wl_store(_wl_store)
    user_set_auth_store(_auth_store)
    admin_set_stores(_auth_store, _wl_store)
    cp_set_auth_store(_auth_store)
    ds_set_auth_store(_auth_store)

    # 数据迁移：将现有数据绑定到 admin 用户（需先解析 admin，供内置 provider 迁移取其加密 Key）
    admin_user = admin or _auth_store.get_user_by_username("admin")
    if admin_user:
        run_migration(admin_user.id)

    # 统一 Provider 管理：把已配置 Key 的内置 provider 迁入 custom_providers 表（唯一真源，幂等）
    try:
        from bottleneck_hunter.web.provider_migration import migrate_builtin_providers_to_custom
        migrate_builtin_providers_to_custom(
            _auth_store, _wl_store,
            admin_user_id=(admin_user.id if admin_user else ""),
        )
    except Exception as e:
        logging.getLogger(__name__).warning("内置 provider 统一迁移失败: %s", e)

    # 加载全部 provider（含迁移来的原内置）到 factory 运行时缓存
    from bottleneck_hunter.llm_clients.factory import register_custom_provider
    for cp in _auth_store.list_custom_providers():
        detail = _auth_store.get_custom_provider(cp["provider_id"])
        if detail and detail.get("is_active"):
            api_key = ""
            if detail.get("api_key_encrypted"):
                try:
                    from bottleneck_hunter.auth.crypto import decrypt
                    api_key = decrypt(detail["api_key_encrypted"])
                except Exception:
                    pass
            register_custom_provider(
                cp["provider_id"], cp["base_url"], api_key, cp["default_model"],
            )

    # 加载全局 provider 覆盖（默认模型/base_url）到 factory 运行时缓存
    try:
        from bottleneck_hunter.llm_clients.factory import refresh_provider_overrides
        refresh_provider_overrides()
    except Exception:
        pass

    wl_set_store(_wl_store)
    wl_set_auth_store(_auth_store)
    dc_set_store(_wl_store)
    st_set_store(_wl_store)
    aic_set_store(_wl_store)
    aic_set_auth_store(_auth_store)
    reverse_set_store(_wl_store)
    settings_set_stores(_wl_store, _auth_store)
    data_report_set_stores(_wl_store, _auth_store)
    from bottleneck_hunter.data_provider.hub import set_stats_store
    set_stats_store(_wl_store)
    init_broadcaster()
    scheduler = init_scheduler(_wl_store, auth_store=_auth_store)
    if scheduler:
        scheduler.start()
        logging.getLogger(__name__).info("Watchlist scheduler started")
    yield
    shutdown_scheduler()
    shutdown_broadcaster()
    await close_http_client()


# ── ASGI 认证中间件 ───────────────────────────────────────

# 不需要认证的路径前缀
_PUBLIC_PREFIXES = ("/login", "/static/", "/api/auth/")


class AuthMiddleware:
    """ASGI 中间件：JWT cookie 认证。

    - 公开路径直接放行
    - API 请求（/api/）未认证返回 401 JSON
    - 页面请求未认证 302 到 /login
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_public = any(path.startswith(p) for p in _PUBLIC_PREFIXES)

        # 从 cookie 中提取 JWT（对所有路径都尝试解析，以便公开路径也能拿到 user 信息）
        cookie_name = get_cookie_name()
        user_payload = None
        headers = dict(scope.get("headers", []))
        cookie_header = headers.get(b"cookie", b"").decode()
        if cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith(f"{cookie_name}="):
                    token = part[len(cookie_name) + 1:]
                    user_payload = verify_token(token)
                    break

        # 验证用户是否活跃
        if user_payload:
            user_db = _auth_store.get_user_by_id(user_payload.get("sub", ""))
            if not user_db or not user_db.is_active:
                user_payload = None

        if user_payload:
            # 注入用户信息到 scope.state
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["user"] = user_payload
            await self.app(scope, receive, send)
            return

        # 公开路径：即使未认证也放行（不注入 user）
        if is_public:
            await self.app(scope, receive, send)
            return

        # 未认证
        if path.startswith("/api/"):
            # API 请求返回 401 JSON
            body = json.dumps({"detail": "未登录"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
        else:
            # 页面请求 302 到 /login
            await send({
                "type": "http.response.start",
                "status": 302,
                "headers": [(b"location", b"/login")],
            })
            await send({"type": "http.response.body", "body": b""})


class NoCacheStaticMiddleware:
    """Pure ASGI middleware — does NOT buffer streaming responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/static/"):
            async def send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    headers.append((b"cache-control", b"no-cache, no-store, must-revalidate"))
                    message = {**message, "headers": headers}
                await send(message)
            await self.app(scope, receive, send_with_no_cache)
        else:
            await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="BottleneckHunter", version="0.1.0", lifespan=lifespan)

    # 中间件注册顺序：最后注册的最先执行
    # AuthMiddleware → NoCacheStaticMiddleware → FastAPI
    app.add_middleware(NoCacheStaticMiddleware)
    app.add_middleware(AuthMiddleware)

    app.include_router(auth_router)  # /api/auth/*
    app.include_router(router, prefix="/api")
    app.include_router(watchlist_router, prefix="/api/watchlist")
    app.include_router(decision_router, prefix="/api/decision")
    app.include_router(trading_router, prefix="/api/trading")
    app.include_router(user_router, prefix="/api/user")
    app.include_router(admin_router, prefix="/api/admin")
    app.include_router(syslog_router, prefix="/api/system")
    app.include_router(custom_provider_router, prefix="/api/custom-providers")
    app.include_router(data_source_router, prefix="/api/data-sources")
    app.include_router(data_report_router, prefix="/api/data-report")
    app.include_router(ai_config_router, prefix="/api/ai-config")
    app.include_router(reverse_router, prefix="/api/reverse")
    app.include_router(settings_router, prefix="/api/settings")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        resp = FileResponse(str(STATIC_DIR / "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    @app.get("/login")
    async def login_page():
        resp = FileResponse(str(STATIC_DIR / "login.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    return app
