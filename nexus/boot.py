from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from nexus.middleware import ServiceAuthMiddleware

DEFAULT_SERVICE_AUTH_WHITELIST: tuple[str, ...] = (
    "/health",
    "/health/detailed",
    "/api/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/",
    "/login",
    "/static",
)

HealthCheck = Callable[[], Awaitable[str]]


def register_service_auth(
    app: FastAPI,
    *,
    whitelist_paths: Optional[list[str]] = None,
    public_api_prefixes: Optional[list[str]] = None,
) -> None:
    app.add_middleware(
        ServiceAuthMiddleware,
        whitelist_paths=whitelist_paths or list(DEFAULT_SERVICE_AUTH_WHITELIST),
        public_api_prefixes=public_api_prefixes or [],
    )


def register_health_detail(
    app: FastAPI,
    *,
    app_name: str,
    app_version: str,
    checks: dict[str, HealthCheck],
) -> None:
    @app.get("/health/detailed")
    async def health_detailed() -> dict[str, object]:
        results: dict[str, str] = {}
        overall: bool = True
        for name, check in checks.items():
            try:
                status: str = await check()
                results[name] = status
                if status != "ok":
                    overall = False
            except Exception:
                results[name] = "error"
                overall = False
        return {
            "status": "ok" if overall else "degraded",
            "app": app_name,
            "version": app_version,
            "checks": results,
        }


def mount_spa_static(
    app: FastAPI,
    directory: str,
    *,
    index: str = "index.html",
    login: str = "login.html",
) -> None:
    static_path: Path = Path(directory)
    if not static_path.exists():
        static_path.mkdir(parents=True, exist_ok=True)

    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

    @app.get("/")
    async def index_route() -> FileResponse:
        return FileResponse(str(static_path / index))

    @app.get("/login")
    async def login_route() -> FileResponse:
        login_file: Path = static_path / login
        if login_file.exists():
            return FileResponse(str(login_file))
        return FileResponse(str(static_path / index))