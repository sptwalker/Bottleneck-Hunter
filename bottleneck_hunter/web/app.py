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

# 全系统统一北京时间：设置进程时区，使所有 naive datetime.now()/date.today() 及日志时间按北京解析。
# 显式 aware UTC 存储（_now_iso）与 APScheduler 的显式 Asia/Shanghai 触发器不受影响。
# Linux（生产 Docker）经 tzset 生效；Windows 无 tzset，本地开发忽略。
os.environ["TZ"] = "Asia/Shanghai"
try:
    import time as _time
    _time.tzset()
except AttributeError:
    pass

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

    # 加载全部 provider 的**元数据**（base_url/default_model）到 factory 运行时缓存。
    # 严格隔离：绝不解密/缓存任何 api_key —— KEY 一律按当前用户从加密表实时解析。
    from bottleneck_hunter.llm_clients.factory import register_custom_provider
    _all_custom_providers = _auth_store.list_custom_providers()
    for cp in _all_custom_providers:
        detail = _auth_store.get_custom_provider(cp["provider_id"])
        if detail and detail.get("is_active"):
            register_custom_provider(
                cp["provider_id"], cp["base_url"], default_model=cp["default_model"],
            )

    # 推送「禁用集合 + 主要 provider」到 factory 运行时状态（禁用/主要生效于解析层）
    try:
        from bottleneck_hunter.llm_clients.factory import set_provider_status
        _inactive = [c["provider_id"] for c in _all_custom_providers if not c.get("is_active")]
        _primary = next((c["provider_id"] for c in _all_custom_providers if c.get("is_primary")), "")
        set_provider_status(_inactive, _primary)
    except Exception as e:
        logging.getLogger(__name__).debug("加载 provider 启用/主要状态失败: %s", e)

    # 一次性迁移历史全局 KEY（.env + custom_providers 全局密钥）→ admin 用户级存储，然后清除全局。
    try:
        from bottleneck_hunter.web.key_isolation_migration import migrate_global_keys_to_admin
        migrate_global_keys_to_admin(_auth_store, admin_user_id=(admin_user.id if admin_user else ""))
    except Exception as e:
        logging.getLogger(__name__).warning("全局 KEY 迁移失败: %s", e)

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
    from bottleneck_hunter.data_provider.scheduler import set_store as ds_set_store
    ds_set_store(_wl_store)  # 供调度器 per-day 额度阀查 datasource_stats
    from bottleneck_hunter.web.oplog import set_store as oplog_set_store
    oplog_set_store(_wl_store)
    from bottleneck_hunter.web.oplog_api import set_store as oplog_api_set_store
    oplog_api_set_store(_wl_store)
    from bottleneck_hunter.web.translate import set_store as translate_set_store
    translate_set_store(_wl_store)
    init_broadcaster()
    # 企业档案一次性回填（后台线程，不阻塞启动；幂等，只跑一次）
    try:
        import threading
        from bottleneck_hunter.dataflows.store import AnalysisStore
        threading.Thread(target=lambda: AnalysisStore().backfill_company_archive(),
                         daemon=True, name="archive-backfill").start()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("企业档案回填线程启动失败", exc_info=True)
    # 复位被上次异常退出（SIGKILL/OOM/重启）卡在 running 的管线状态，避免 UI/门控误判仍在跑。
    try:
        n = _wl_store.reconcile_running_pipelines()
        if n:
            logging.getLogger(__name__).warning("启动复位 %d 条中断的 running 管线状态 → interrupted", n)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("管线状态复位失败", exc_info=True)
    scheduler = init_scheduler(_wl_store, auth_store=_auth_store)
    if scheduler:
        scheduler.start()
        logging.getLogger(__name__).info("Watchlist scheduler started")
        # 启动补跑：重启后立即刷新数据源健康巡检（治「停机跨过巡检点 → 当次漏检」）
        try:
            from datetime import datetime, timedelta, timezone as _tz
            from bottleneck_hunter.watchlist.scheduler import job_datasource_report
            scheduler.add_job(job_datasource_report, "date",
                              run_date=datetime.now(_tz.utc) + timedelta(seconds=30),
                              id="datasource_report_startup", name="Datasource health (startup catch-up)",
                              replace_existing=True)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).debug("数据源健康巡检启动补跑注册失败", exc_info=True)
    yield
    shutdown_scheduler()
    shutdown_broadcaster()
    await close_http_client()


# ── ASGI 认证中间件 ───────────────────────────────────────

# 不需要认证的路径前缀
_PUBLIC_PREFIXES = ("/login", "/static/", "/api/auth/", "/healthz")


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
            # 设置请求级「当前用户」上下文：供下游 LLM/数据源 KEY 严格按用户解析
            from bottleneck_hunter.auth.current_user import set_current_user, reset_current_user
            _uctx = set_current_user(user_payload.get("sub", ""))
            try:
                await self.app(scope, receive, send)
            finally:
                reset_current_user(_uctx)
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

    # 严格隔离：用户未配置 KEY → 统一 400 友好提示（而非 500）
    from fastapi.responses import JSONResponse
    from bottleneck_hunter.llm_clients.factory import MissingUserKeyError

    @app.exception_handler(MissingUserKeyError)
    async def _missing_key_handler(request, exc):  # noqa: ANN001
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": "missing_user_key"})

    # 中间件注册顺序：最后注册的最先执行
    # AuthMiddleware → NoCacheStaticMiddleware → OpLogMiddleware → FastAPI
    # OpLogMiddleware 放最内层：AuthMiddleware 已在 scope.state 注入 user，它才能按用户记操作日志
    from bottleneck_hunter.web.oplog import OpLogMiddleware
    app.add_middleware(OpLogMiddleware)
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
    from bottleneck_hunter.web.egress_api import router as egress_router
    app.include_router(egress_router, prefix="/api/egress")
    from bottleneck_hunter.web.oplog_api import router as oplog_router
    app.include_router(oplog_router, prefix="/api/oplog")
    from bottleneck_hunter.web.translate_api import router as translate_router
    app.include_router(translate_router, prefix="/api/translate")
    from bottleneck_hunter.web.vip_api import router as vip_router, set_store as vip_set_store
    vip_set_store(_wl_store)
    app.include_router(vip_router, prefix="/api/vip")

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

    @app.get("/healthz")
    async def healthz():
        """就绪探针：验证调度器存活 + DB 可读，而非仅进程活着。

        旧 healthcheck 打 /（静态文件）→ 调度器死了/DB 锁死仍返回绿灯，掩盖故障。
        """
        from fastapi.responses import JSONResponse
        from bottleneck_hunter.watchlist.scheduler import is_scheduler_running
        checks = {"scheduler": is_scheduler_running()}
        try:
            _wl_store.count_by_tier()  # watchlist.db 轻量读（COUNT，不全表拉取）
            checks["db"] = True
        except Exception:
            checks["db"] = False
        try:
            # analyses.db 是独立文件——单独探一次，避免它锁死/损坏时 healthz 仍绿灯。
            # 复用 api 模块的单例 _store，避免每次健康检查都 new AnalysisStore() 触发 _init_db
            # （每 30s 一条"分析数据库已就绪"日志刷屏）。
            from bottleneck_hunter.web.api import _store as _analysis_store
            _analysis_store.ping()
            checks["analyses_db"] = True
        except Exception:
            checks["analyses_db"] = False
        ok = all(checks.values())
        return JSONResponse({"ok": ok, "checks": checks}, status_code=200 if ok else 503)

    return app
