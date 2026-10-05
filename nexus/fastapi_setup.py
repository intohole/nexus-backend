"""FastAPI 应用装配：生命周期与中间件栈注册。

内部端点/健康检查见 nexus.internal_endpoints，静态资源见 nexus.static_files，
公共符号在本模块再导出（业务仓统一从 nexus.fastapi_setup 导入）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Callable, Optional

from fastapi import FastAPI

from nexus.config import NexusConfig, get_settings
from nexus.database import close_db, init_db
from nexus.internal_endpoints import (
    register_internal_endpoints,
    require_service_token,
    setup_health_check,
)
from nexus.logging import get_logger, setup_logging
from nexus.middleware import (
    ErrorHandlerMiddleware,
    LoadingSplashMiddleware,
    LoggingMiddleware,
    NoCacheMiddleware,
    NotFoundCheckMiddleware,
    RequestIdMiddleware,
    setup_cors,
)
from nexus.middleware_base import SecurityHeadersMiddleware
from nexus.rate_limit import RateLimitMiddleware
from nexus.static_files import setup_static_files
from nexus.utils import HealthRegistry

__all__ = [
    "AppLifecycle", "create_app", "setup_middleware",
    "require_service_token", "register_internal_endpoints",
    "setup_health_check", "setup_static_files",
]


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

    async def __aenter__(self) -> "AppLifecycle":
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
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
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
    enable_loading_splash: bool = False,
) -> FastAPI:
    cfg: NexusConfig = config or get_settings()
    setup_logging(cfg, app_name=title)

    if lifespan is None:
        lifespan = AppLifecycle(cfg)

        @asynccontextmanager
        async def _close_lion_pool() -> AsyncGenerator[None, None]:
            from nexus.lion import get_lion

            await get_lion().aclose()
            yield

        lifespan.add_shutdown_hook(_close_lion_pool)

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
    if enable_loading_splash:
        app.add_middleware(LoadingSplashMiddleware, app_name=cfg.app_name)

    from nexus.middleware_exception import setup_exception_handlers
    setup_exception_handlers(app)

    setup_cors(app, cfg)

    setup_health_check(app, lifespan._health_registry, cfg)

    return app


def setup_middleware(
    app: FastAPI,
    config: Optional[NexusConfig] = None,
    *,
    enable_cors: bool = True,
    cors_origins: Optional[list[str]] = None,
    enable_rate_limit: bool = True,
    enable_logging: bool = True,
    enable_error_handler: bool = True,
    enable_no_cache: bool = True,
    enable_request_id: bool = True,
    enable_security_headers: bool = False,
    enable_not_found_check: bool = False,
    enable_loading_splash: bool = False,
    enable_audit: bool = False,
    no_cache_prefix: str = "/static",
) -> None:
    """在已存在的 FastAPI app 上注册统一中间件栈。

    适用于无法直接替换为 create_app() 的项目（已有自定义 lifespan / 路由 / 异常体系）。
    消除各项目 main.py 中重复的 add_middleware 样板代码。

    用法：
        app = FastAPI(lifespan=my_lifespan)
        setup_middleware(app, config, enable_security_headers=True)
        app.include_router(my_router)
    """
    cfg: NexusConfig = config or get_settings()

    if enable_request_id:
        app.add_middleware(RequestIdMiddleware)
    if enable_logging:
        app.add_middleware(LoggingMiddleware)
    if enable_rate_limit:
        app.add_middleware(RateLimitMiddleware, config=cfg)
    if enable_error_handler:
        app.add_middleware(ErrorHandlerMiddleware)
    if enable_no_cache:
        app.add_middleware(NoCacheMiddleware, path_prefix=no_cache_prefix)
    if enable_security_headers:
        app.add_middleware(SecurityHeadersMiddleware)
    if enable_not_found_check:
        app.add_middleware(NotFoundCheckMiddleware)
    if enable_loading_splash:
        app.add_middleware(LoadingSplashMiddleware, app_name=cfg.app_name)
    if enable_audit:
        from nexus.audit_middleware import AuditMiddleware
        app.add_middleware(AuditMiddleware, config=cfg)
    if enable_cors:
        setup_cors(app, cfg, origins=cors_origins)

    if enable_error_handler:
        from nexus.middleware_exception import setup_exception_handlers
        setup_exception_handlers(app)
