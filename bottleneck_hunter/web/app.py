"""FastAPI application factory for BottleneckHunter web UI."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

from bottleneck_hunter.web.api import router
from bottleneck_hunter.web.watchlist_api import router as watchlist_router, set_store as wl_set_store
from bottleneck_hunter.web.decision_api import router as decision_router, set_store as dc_set_store
from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.scheduler import init_scheduler, shutdown_scheduler

STATIC_DIR = Path(__file__).parent / "static"

_wl_store = WatchlistStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    wl_set_store(_wl_store)
    dc_set_store(_wl_store)
    scheduler = init_scheduler(_wl_store)
    if scheduler:
        scheduler.start()
        logging.getLogger(__name__).info("Watchlist scheduler started")
    yield
    shutdown_scheduler()


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
    app.add_middleware(NoCacheStaticMiddleware)
    app.include_router(router, prefix="/api")
    app.include_router(watchlist_router, prefix="/api/watchlist")
    app.include_router(decision_router, prefix="/api/decision")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        resp = FileResponse(str(STATIC_DIR / "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    return app
