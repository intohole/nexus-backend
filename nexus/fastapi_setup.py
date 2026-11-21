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
    StaticAssetsCacheMiddleware,
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


def _mount_component(app: FastAPI, name: str, cfg: NexusConfig, cors_origins, no_cache_prefix: str) -> None:
    """装配单个 stack 组件；exception_handlers 为处理器注册，与栈位无关。"""
    if name == "cors":
        setup_cors(app, cfg, origins=cors_origins)
    elif name == "request_id":
        app.add_middleware(RequestIdMiddleware)
    elif name == "logging":
        app.add_middleware(LoggingMiddleware)
    elif name == "rate_limit":
        app.add_middleware(RateLimitMiddleware, config=cfg)
    elif name == "error_handler":
        app.add_middleware(ErrorHandlerMiddleware)
    elif name == "no_cache":
        app.add_middleware(StaticAssetsCacheMiddleware, path_prefix=no_cache_prefix)
    elif name == "security_headers":
        app.add_middleware(SecurityHeadersMiddleware)
    elif name == "not_found_check":
        app.add_middleware(NotFoundCheckMiddleware)
    elif name == "loading_splash":
        app.add_middleware(LoadingSplashMiddleware, app_name=cfg.app_name)
    elif name == "audit":
        from nexus.audit_middleware import AuditMiddleware
        app.add_middleware(AuditMiddleware, config=cfg)
    elif name == "metrics":
        try:
            from nexus.middleware_metrics import RequestMetricsMiddleware
            app.add_middleware(RequestMetricsMiddleware)
        except ImportError:
            get_logger("nexus.setup").warning(
                "nexus.middleware_metrics 缺失，指标中间件降级跳过"
            )
    elif name == "exception_handlers":
        from nexus.middleware_exception import setup_exception_handlers
        setup_exception_handlers(app)
    else:
        raise ValueError(f"unknown middleware stack component: {name!r}")


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
        try:
            from nexus.request_metrics import get_request_metrics
            get_request_metrics().flush()
        except Exception as exc:
            logger.warning("request metrics final flush failed: %s", str(exc))
        try:
            from nexus.llm_metrics import get_llm_metrics
            get_llm_metrics().flush_to_store()
        except Exception as exc:
            logger.warning("llm metrics final flush failed: %s", str(exc))
        try:
            from nexus.scheduler import get_scheduler
            get_scheduler().shutdown(wait=False)
        except Exception as exc:
            logger.warning("scheduler shutdown on app exit failed: %s", str(exc))
        await close_db()
        logger.info("Application shutdown complete")


def create_app(
    title: str = "App",
    config: Optional[NexusConfig] = None,
    lifespan: Optional[AppLifecycle] = None,
    enable_rate_limit: bool = True,
    enable_logging_middleware: bool = True,
    enable_loading_splash: bool = False,
    enable_metrics: bool = True,
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

    if enable_logging_middleware:
        app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)
    if enable_rate_limit and cfg.rate_limit.enabled:
        app.add_middleware(RateLimitMiddleware, config=cfg)
    app.add_middleware(ErrorHandlerMiddleware)
    app.add_middleware(StaticAssetsCacheMiddleware, path_prefix="/static")
    if enable_loading_splash:
        app.add_middleware(LoadingSplashMiddleware, app_name=cfg.app_name)
    if enable_metrics:
        from nexus.middleware_metrics import RequestMetricsMiddleware
        app.add_middleware(RequestMetricsMiddleware)

    from nexus.middleware_exception import setup_exception_handlers
    setup_exception_handlers(app)

    setup_cors(app, cfg)

    setup_health_check(app, lifespan._health_registry, cfg)

    return app


def setup_middleware(
    app: FastAPI,
    config: Optional[NexusConfig] = None,
    *,
    stack: Optional[list[str]] = None,
    enable_cors: bool = True,
    cors_origins: Optional[list[str]] = None,
    enable_rate_limit: bool = True,
    enable_logging: bool = True,
    enable_error_handler: bool = True,
    enable_exception_handlers: Optional[bool] = None,
    enable_no_cache: bool = True,
    enable_request_id: bool = True,
    enable_security_headers: bool = False,
    enable_not_found_check: bool = False,
    enable_loading_splash: bool = False,
    enable_audit: bool = False,
    enable_metrics: bool = True,
    no_cache_prefix: str = "/static",
) -> None:
    """在已存在的 FastAPI app 上注册统一中间件栈。

    适用于无法直接替换为 create_app() 的项目（已有自定义 lifespan / 路由 / 异常体系）。

    两种用法二选一：
    - stack=[...]：显式声明栈面组件，外→内顺序（跨仓克隆族直替面，运行栈逐字节
      等价仓内手搓面）；组件全集见 _mount_component，exception_handlers 为处理器
      注册与栈位无关。
    - 缺省 flags 路径：canonical 默认栈（enable_* 开关；Logging 先加、RequestId
      后加=r222 判据「外层先设 id」）。
    """
    cfg: NexusConfig = config or get_settings()

    if stack is not None:
        for name in reversed(list(stack)):
            _mount_component(app, name, cfg, cors_origins, no_cache_prefix)
        return

    components: list[str] = []
    if enable_logging:
        components.append("logging")
    if enable_request_id:
        components.append("request_id")
    if enable_rate_limit and cfg.rate_limit.enabled:
        components.append("rate_limit")
    if enable_error_handler:
        components.append("error_handler")
    if enable_no_cache:
        components.append("no_cache")
    if enable_security_headers:
        components.append("security_headers")
    if enable_not_found_check:
        components.append("not_found_check")
    if enable_loading_splash:
        components.append("loading_splash")
    if enable_audit:
        components.append("audit")
    if enable_metrics:
        components.append("metrics")
    if enable_cors:
        components.append("cors")
    with_exception_handlers = (
        enable_exception_handlers
        if enable_exception_handlers is not None
        else enable_error_handler
    )
    if with_exception_handlers:
        components.append("exception_handlers")

    for name in components:
        _mount_component(app, name, cfg, cors_origins, no_cache_prefix)
