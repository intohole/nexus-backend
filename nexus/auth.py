"""认证依赖：JWT/UC SDK 鉴权与当前用户信息提取（鉴权组合器见 nexus.permissions）。"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context
from nexus.logging import get_logger
from nexus.middleware_base import TokenCache
from nexus.uc_sdk_helper import set_auth_injector as _set_uc_auth_injector
from nexus.user_display import resolve_display_name

logger = get_logger("nexus.auth")

_security: HTTPBearer = HTTPBearer(auto_error=False)
_uc_sdk_ready: bool = False
_TOKEN_CACHE_TTL: int = 60
_TOKEN_CACHE_MAXSIZE: int = 500
DELEGATION_HEADER: str = "X-On-Behalf-Of"
_DELEGATED_ID_RE: re.Pattern = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AuthDependencies:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._sdk: Optional[object] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ready: bool = False
        self._token_cache: TokenCache = TokenCache(maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_CACHE_TTL)

    def set_sdk(self, sdk: object) -> None:
        """注入外部创建的 UC SDK，跳过 AuthDependencies 内部的懒创建。

        替代各应用中的 _ensure_nexus_configured() env 黑科技。
        """
        self._sdk = sdk
        self._ready = True
        logger.info("UC SDK injected externally, env-var bootstrap skipped")

    async def get_sdk(self) -> Optional[object]:
        if self._sdk is not None and self._ready:
            return self._sdk
        async with self._lock:
            if self._sdk is not None and self._ready:
                return self._sdk
            if self._sdk is None:
                from nexus.uc_sdk_helper import get_uc_sdk

                try:
                    self._sdk = get_uc_sdk()
                    self._ready = True
                    return self._sdk
                except RuntimeError:
                    try:
                        from nexus.uc_sdk import UserCenterSDK

                        uc_cfg = self._config.uc
                        self._sdk = UserCenterSDK(
                            base_url=uc_cfg.base_url,
                            app_key=uc_cfg.app_key,
                            app_secret=uc_cfg.app_secret,
                            jwt_secret_key=uc_cfg.jwt_secret,
                        )
                    except ImportError:
                        logger.warning("usercenter SDK not installed, auth disabled")
                        self._sdk = None
                        return None
            if not self._ready:
                from nexus.uc_sdk_helper import bootstrap_sdk

                uc_cfg = self._config.uc
                if not uc_cfg.app_key or not uc_cfg.app_secret:
                    self._ready = True
                    logger.info("UC SDK ready without bootstrap (no app_key/app_secret)")
                else:
                    await bootstrap_sdk(self._sdk)
                    self._ready = True
            return self._sdk if self._ready else None

    async def validate_token(self, token: str) -> Optional[dict[str, object]]:
        if not token or not token.strip():
            return None
        cached: Optional[dict[str, object]] = self._token_cache.get(token)
        if cached is not None:
            self._apply_request_context(cached)
            return cached

        sdk: Optional[object] = await self.get_sdk()
        if sdk is None:
            return None
        try:
            result: dict[str, object] = await sdk.verify_token(token)
            if result and result.get("success", True) is not False:
                self._apply_request_context(result)
                self._token_cache.set(token, result)
                return result
            return None
        except Exception as exc:
            logger.warning("Token validation failed: %s", str(exc))
            return None

    @staticmethod
    def _apply_request_context(user: dict[str, object]) -> None:
        user_id_raw: object = user.get("user_id")
        org_id_raw: object = user.get("org_id")
        if user_id_raw is not None:
            set_request_context(user_id=str(user_id_raw))
        if org_id_raw is not None:
            set_request_context(org_id=str(org_id_raw))

    @staticmethod
    def _is_service_principal(result: dict[str, object]) -> bool:
        uid: str = str(result.get("user_id") or "")
        role: str = str(result.get("role") or "")
        return uid.startswith("app_") or role == "service"

    async def _resolve_delegation(
        self,
        request: Optional[Request],
        result: dict[str, object],
        *,
        required: bool,
    ) -> Optional[str]:
        """服务身份委托解析：服务 JWT + X-On-Behalf-Of = 平台代表用户调用。

        fail-closed：委托头只在验签通过的服务身份下被消费；无委托头时
        required 端点 403（服务凭证不得当作用户身份透传业务端点），
        optional 端点按匿名处理（None）。
        """
        if not self._is_service_principal(result):
            return None
        header: str = ""
        if request is not None:
            header = (request.headers.get(DELEGATION_HEADER) or "").strip()
        if not header:
            if required:
                raise HTTPException(
                    status_code=403,
                    detail="service principal requires X-On-Behalf-Of on user endpoints",
                )
            return None
        if not _DELEGATED_ID_RE.fullmatch(header):
            raise HTTPException(status_code=403, detail="invalid X-On-Behalf-Of")
        set_request_context(user_id=header)
        return header

    async def get_user_id_required(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
        request: Optional[Request] = None,
    ) -> str:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        sdk: Optional[object] = await self.get_sdk()
        if sdk is None:
            raise HTTPException(status_code=503, detail="认证服务不可用")
        result: Optional[dict[str, object]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        delegated: Optional[str] = await self._resolve_delegation(
            request, result, required=True
        )
        if delegated is not None:
            return delegated
        user_id_raw: object = result.get("user_id")
        if not user_id_raw:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return str(user_id_raw)

    async def get_user_id_optional(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
        request: Optional[Request] = None,
    ) -> Optional[str]:
        if credentials is None:
            return None
        result: Optional[dict[str, object]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            return None
        if self._is_service_principal(result):
            return await self._resolve_delegation(request, result, required=False)
        user_id_raw: object = result.get("user_id")
        return str(user_id_raw) if user_id_raw is not None else None

    async def get_user_full(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> dict[str, object]:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        sdk: Optional[object] = await self.get_sdk()
        if sdk is None:
            raise HTTPException(status_code=503, detail="认证服务不可用")
        result: Optional[dict[str, object]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return result


_auth_deps: Optional[AuthDependencies] = None


def get_auth_deps() -> AuthDependencies:
    global _auth_deps
    if _auth_deps is None:
        _auth_deps = AuthDependencies()
    return _auth_deps


def configure_uc_sdk(sdk: object) -> None:
    """将外部创建的 UC SDK 注入 AuthDependencies 单例。

    替代各应用中 _ensure_nexus_configured() 的 env-var 黑科技，
    消除 AuthDependencies 与 app 各自创建 SDK 的双重实例问题。
    """
    get_auth_deps().set_sdk(sdk)


_set_uc_auth_injector(configure_uc_sdk)


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization 头提取 Bearer token。"""
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization[7:]
    if authorization.startswith("bearer "):
        return authorization[7:]
    token = authorization.strip()
    return token or None


async def get_current_user_id_required(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> str:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_id_required(credentials, request)


async def get_current_user_id_optional(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> Optional[str]:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_id_optional(credentials, request)


async def get_current_user_full(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> dict[str, object]:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_full(credentials)


def normalize_user_dict(user: dict[str, object]) -> dict[str, object]:
    return {
        "user_id": str(user.get("user_id", "")),
        "app_id": user.get("app_id"),
        "org_id": user.get("org_id"),
        "role": user.get("role", "user"),
        "vip_level": user.get("vip_level", 0),
        "display_name": user.get("display_name", ""),
        "nickname": user.get("nickname", ""),
        "username": user.get("username", ""),
        "full_name": user.get("full_name", ""),
        "resolved_display_name": resolve_display_name(user),
    }


async def get_current_user_full_normalized(
    user: dict[str, object] = Depends(get_current_user_full),
) -> dict[str, object]:
    return normalize_user_dict(user)


def get_user_string_id(user_id: Optional[str]) -> str:
    if user_id is None:
        return "guest_none"
    return f"user_{user_id}"


def parse_user_id(user_id: str | int) -> int:
    if isinstance(user_id, int):
        return user_id
    try:
        return int(user_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid user_id format")
