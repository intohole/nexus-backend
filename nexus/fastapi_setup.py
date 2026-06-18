from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nexus.config import NexusConfig, get_settings
from nexus.database import close_db, init_db
from nexus.logging import get_logger, setup_logging
from nexus.middleware import (
    ErrorHandlerMiddleware,
    LoggingMiddleware,
    NoCacheMiddleware,
    RequestIdMiddleware,
    setup_cors,
)
from nexus.rate_limit import RateLimitMiddleware
from nexus.utils import HealthRegistry


class AppLifecycle:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._startup_hooks: list[Callable[[], AsyncGenerator[None, None]]] = []
        self._shutdown_hooks: list[Callable[[], AsyncGenerator[None, None]]] = []
        self._health_registry: HealthRegistry = HealthRegistry()

    def add_startup_hook(
        self, hook: Callable[[], AsyncGenerator[None, None]]
    ) -> None:
        self._startup_hooks.append(hook)

    def add_shutdown_hook(
        self, hook: Callable[[], AsyncGenerator[None, None]]
    ) -> None:
        self._shutdown_hooks.append(hook)

    def add_health_check(
        self, name: str, check_func: Callable[[], object]
    ) -> None:
        self._health_registry.register(name, check_func)

    @asynccontextmanager
    async def __aenter__(self) -> AsyncGenerator[None, None]:
        logger = get_logger("nexus.lifecycle")
        logger.info("Application starting up...")
        await init_db()
        for hook in self._startup_hooks:
            try:
                async for _ in hook():
                    pass
            except Exception as exc:
                logger.error("Startup hook failed: %s", str(exc))
        logger.info("Application started successfully")

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        logger = get_logger("nexus.lifecycle")
        logger.info("Application shutting down...")
        for hook in self._shutdown_hooks:
            try:
                async for _ in hook():
                    pass
            except Exception as exc:
                logger.error("Shutdown hook failed: %s", str(exc))
        await close_db()
        logger.info("Application shutdown complete")


def create_app(
    title: str = "App",
    config: Optional[NexusConfig] = None,
    lifespan: Optional[AppLifecycle] = None,
    enable_rate_limit: bool = True,
    enable_logging_middleware: bool = True,
) -> FastAPI:
    cfg: NexusConfig = config or get_settings()
    setup_logging(cfg, app_name=title)

    if lifespan is None:
        lifespan = AppLifecycle(cfg)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        async with lifespan:
            yield

    app: FastAPI = FastAPI(
        title=title,
        version=cfg.app_version,
        debug=cfg.debug,
        lifespan=app_lifespan,
    )

    app.add_middleware(RequestIdMiddleware)
    if enable_logging_middleware:
        app.add_middleware(LoggingMiddleware)
    if enable_rate_limit:
        app.add_middleware(RateLimitMiddleware, config=cfg)
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(NoCacheMiddleware, path_prefix="/static")

    setup_cors(app, cfg)

    setup_health_check(app, lifespan._health_registry, cfg)

    return app


def setup_health_check(
    app: FastAPI,
    registry: HealthRegistry,
    config: NexusConfig,
) -> None:
    @app.get("/health")
    async def health_check() -> dict[str, object]:
        return {
            "status": "healthy",
            "app": config.app_name,
            "version": config.app_version,
        }

    @app.get("/readiness")
    async def readiness_check() -> JSONResponse:
        checks: dict[str, bool] = await registry.run_all()
        all_healthy: bool = all(checks.values()) if checks else True
        status_code: int = 200 if all_healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ready" if all_healthy else "not ready",
                "checks": checks,
            },
        )


def setup_static_files(
    app: FastAPI,
    directory: Optional[str] = None,
    spa_fallback: Optional[bool] = None,
    path_prefix: Optional[str] = None,
) -> None:
    cfg: NexusConfig = get_settings()
    static_dir: str = directory or cfg.static_files.directory
    spa: bool = spa_fallback if spa_fallback is not None else cfg.static_files.spa_fallback
    prefix: str = path_prefix or cfg.path_prefix

    static_path: Path = Path(static_dir)
    if not static_path.exists():
        static_path.mkdir(parents=True, exist_ok=True)

    mount_path: str = f"{prefix}/static" if prefix else "/static"
    app.mount(mount_path, StaticFiles(directory=str(static_path)), name="static")

    if spa:
        index_path: Path = static_path / "index.html"

        @app.get(f"{prefix}/{{full_path:path}}" if prefix else "/{full_path:path}")
        async def spa_fallback_route(full_path: str) -> FileResponse:
            file_path: Path = static_path / full_path
            if file_path.exists() and file_path.is_file():
                return FileResponse(str(file_path))
            if index_path.exists():
                return FileResponse(str(index_path))
            return JSONResponse(
                status_code=404, content={"detail": "Not Found"}
            )
