from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
from cachetools import TTLCache
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from nexus.logging import get_logger

DEFAULT_WHITELIST_PATHS: Tuple[str, ...] = (
    "/health", "/api/health", "/",
)

DEFAULT_PUBLIC_API_PREFIXES: Tuple[str, ...] = (
    "/api/auth/login", "/api/auth/register", "/api/auth/refresh",
    "/api/auth/config", "/api/auth/login-page-config", "/api/auth/uc/config",
    "/api/vip/levels", "/api/invite-codes/validate", "/api/discovery",
    "/.well-known",
    "/api/gateway/healthz",
)

DEFAULT_STATIC_EXTENSIONS: Tuple[str, ...] = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".html",
)

_JWKS_TTL_SECONDS: int = 300
_JWKS_TIMEOUT_SECONDS: float = 5.0
_USER_TOKEN_CACHE_TTL: int = 60
_USER_TOKEN_CACHE_MAXSIZE: int = 2000


class UserTokenVerifier:
    """用户 JWT 验签：RS256(UC JWKS) 优先，HS256(UC_JWT_SECRET) 兜底，验签通过结果短期缓存。"""

    def __init__(self, base_url: str = "", jwt_secret: str = "") -> None:
        self._base_url: str = (base_url or "").rstrip("/")
        self._jwt_secret: str = jwt_secret or ""
        self._jwks: Dict[str, Dict[str, object]] = {}
        self._jwks_fetched_at: float = 0.0
        self._cache: TTLCache = TTLCache(maxsize=_USER_TOKEN_CACHE_MAXSIZE, ttl=_USER_TOKEN_CACHE_TTL)
        self._logger = get_logger("nexus.user_token")

    @property
    def configured(self) -> bool:
        return bool(self._base_url or self._jwt_secret)

    async def verify(self, token: str) -> bool:
        if not token:
            return False
        cache_key: str = hashlib.sha256(token.encode()).hexdigest()
        if cache_key in self._cache:
            return True
        payload: Optional[Dict[str, object]] = await self._decode(token)
        if payload is None:
            return False
        self._cache[cache_key] = True
        return True

    async def _decode(self, token: str) -> Optional[Dict[str, object]]:
        try:
            from jose import jwt as jose_jwt
        except ImportError:
            self._logger.warning("python-jose 未安装，无法校验用户令牌")
            return None
        try:
            header: Dict[str, object] = jose_jwt.get_unverified_header(token)
        except Exception:
            return None
        algorithm: str = str(header.get("alg", ""))
        if algorithm == "RS256":
            key: object = await self._rsa_key(header.get("kid"))
            if key is None:
                return None
            return self._decode_payload(jose_jwt, token, key, ["RS256"])
        if algorithm == "HS256" and self._jwt_secret:
            return self._decode_payload(jose_jwt, token, self._jwt_secret, ["HS256"])
        return None

    @staticmethod
    def _decode_payload(
        jose_jwt: object, token: str, key: object, algorithms: List[str],
    ) -> Optional[Dict[str, object]]:
        try:
            payload: Dict[str, object] = jose_jwt.decode(token, key, algorithms=algorithms)
        except Exception:
            return None
        if not payload.get("sub"):
            return None
        return payload

    async def _rsa_key(self, kid: object) -> Optional[object]:
        if not self._base_url:
            return None
        if kid not in self._jwks and time.time() - self._jwks_fetched_at > _JWKS_TTL_SECONDS:
            await self._refresh_jwks()
        jwk_data: Optional[Dict[str, object]] = self._jwks.get(kid)
        if jwk_data is None:
            await self._refresh_jwks()
            jwk_data = self._jwks.get(kid)
        if jwk_data is None:
            return None
        try:
            from jose import jwk as jose_jwk
            return jose_jwk.construct(jwk_data)
        except Exception:
            return None

    async def _refresh_jwks(self) -> None:
        self._jwks_fetched_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=_JWKS_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._base_url}/.well-known/jwks.json")
            if response.status_code != 200:
                self._logger.warning("UC JWKS 拉取失败: status=%s", response.status_code)
                return
            keys: List[Dict[str, object]] = response.json().get("keys", [])
            self._jwks = {str(k.get("kid")): k for k in keys if k.get("kid")}
            self._logger.info("UC JWKS 已加载: keys=%s", len(self._jwks))
        except Exception as exc:
            self._logger.warning("UC JWKS 拉取异常: %s", exc)


def build_default_verifier() -> UserTokenVerifier:
    base_url: str = os.environ.get("UC__BASE_URL", "") or os.environ.get("UC_BASE_URL", "")
    jwt_secret: str = os.environ.get("UC__JWT_SECRET", "") or os.environ.get("UC_JWT_SECRET", "")
    return UserTokenVerifier(base_url=base_url, jwt_secret=jwt_secret)


class ServiceAuthMiddleware(BaseHTTPMiddleware):
    """内部服务认证网关。

    - 服务间调用：X-Service-Token / Bearer<service_token> / service_token cookie 必须与服务令牌完全一致；
    - 终端用户调用（allow_user_tokens=True）：仅放行 UC 真实验签通过的用户 JWT，其余一律 401；
    - 文档路径（/docs /redoc /openapi.json）不属于默认放行范围。
    """

    def __init__(
        self,
        app,
        whitelist_paths: Optional[List[str]] = None,
        public_api_prefixes: Optional[List[str]] = None,
        static_extensions: Optional[List[str]] = None,
        allow_user_tokens: bool = False,
        service_token: Optional[str] = None,
        token_verifier: Optional[UserTokenVerifier] = None,
    ) -> None:
        super().__init__(app)
        self._whitelist: Tuple[str, ...] = tuple(whitelist_paths) if whitelist_paths else DEFAULT_WHITELIST_PATHS
        self._public_prefixes: Tuple[str, ...] = tuple(public_api_prefixes) if public_api_prefixes else DEFAULT_PUBLIC_API_PREFIXES
        self._static_exts: Tuple[str, ...] = tuple(static_extensions) if static_extensions else DEFAULT_STATIC_EXTENSIONS
        self._allow_user_tokens: bool = allow_user_tokens
        self._prefix: str = os.environ.get("PATH_PREFIX", "")
        self._service_token: Optional[str] = service_token
        self._verifier: UserTokenVerifier = token_verifier or build_default_verifier()
        self._logger = get_logger("nexus.service_auth")
        if self._allow_user_tokens and not self._verifier.configured:
            self._logger.warning(
                "allow_user_tokens=True 但未配置 UC_BASE_URL/UC_JWT_SECRET，用户令牌将全部拒绝",
            )

    def _get_service_token(self) -> str:
        if self._service_token is not None:
            return self._service_token
        return os.environ.get("SERVICE_TOKEN", "")

    def _strip_prefix(self, path: str) -> str:
        if self._prefix and path.startswith(self._prefix):
            return path[len(self._prefix):]
        return path

    def _is_public(self, path: str) -> bool:
        if path in self._whitelist or any(path.startswith(p + "/") for p in self._whitelist):
            return True
        if any(path == p or path.startswith(p + "/") for p in self._public_prefixes):
            return True
        if any(path.endswith(ext) for ext in self._static_exts):
            return True
        return False

    @staticmethod
    def _bearer_token(request: Request) -> str:
        auth: str = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    def _extract_token(self, request: Request) -> str:
        header_token: str = request.headers.get("X-Service-Token", "")
        if header_token:
            return header_token
        bearer: str = self._bearer_token(request)
        if bearer:
            return bearer
        return request.cookies.get("service_token", "")

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path: str = self._strip_prefix(request.url.path)
        if self._is_public(path):
            return await call_next(request)

        service_token: str = self._get_service_token()
        if not service_token:
            client_ip: str = request.client.host if request.client else "unknown"
            self._logger.warning(
                "SERVICE_TOKEN not set, denying non-public request: path=%s method=%s ip=%s",
                path, request.method, client_ip,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Service auth not configured"},
            )

        token: str = self._extract_token(request)
        if token and hmac.compare_digest(token, service_token):
            return await call_next(request)

        bearer: str = self._bearer_token(request)
        if self._allow_user_tokens and bearer and await self._verifier.verify(bearer):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        self._logger.warning(
            "Service auth denied: path=%s method=%s ip=%s reason=%s",
            path, request.method, client_ip,
            "invalid_user_token" if bearer else "missing_credential",
        )
        return JSONResponse(status_code=401, content={"detail": "Invalid service token"})