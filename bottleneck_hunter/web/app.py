"""FastAPI application factory for BottleneckHunter web UI."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

load_dotenv()

from bottleneck_hunter.web.api import router

STATIC_DIR = Path(__file__).parent / "static"


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
    app = FastAPI(title="BottleneckHunter", version="0.1.0")
    app.add_middleware(NoCacheStaticMiddleware)
    app.include_router(router, prefix="/api")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        resp = FileResponse(str(STATIC_DIR / "index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    return app
