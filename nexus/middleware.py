"""中间件统一出口：日志/请求 ID/安全头/缓存/认证/启动页等聚合导出。"""
from nexus.middleware_base import (
    REQUEST_ID_HEADER,
    LoggingMiddleware,
    NoCacheMiddleware,
    NotFoundCheckMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    StaticAssetsCacheMiddleware,
    setup_cors,
)
from nexus.middleware_splash import LoadingSplashMiddleware
from nexus.middleware_auth import (
    DEFAULT_PUBLIC_API_PREFIXES,
    DEFAULT_STATIC_EXTENSIONS,
    DEFAULT_WHITELIST_PATHS,
    ServiceAuthMiddleware,
)
from nexus.middleware_exception import (
    ErrorHandlerMiddleware,
    setup_exception_handlers,
)

__all__ = [
    "REQUEST_ID_HEADER",
    "setup_cors",
    "RequestIdMiddleware",
    "NoCacheMiddleware",
    "LoggingMiddleware",
    "NotFoundCheckMiddleware",
    "SecurityHeadersMiddleware",
    "StaticAssetsCacheMiddleware",
    "LoadingSplashMiddleware",
    "ErrorHandlerMiddleware",
    "ServiceAuthMiddleware",
    "setup_exception_handlers",
    "DEFAULT_WHITELIST_PATHS",
    "DEFAULT_PUBLIC_API_PREFIXES",
    "DEFAULT_STATIC_EXTENSIONS",
]
