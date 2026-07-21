from __future__ import annotations

import asyncio
import hashlib
import os
import time
from typing import Awaitable, Callable, Optional

from cachetools import TTLCache
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context
from nexus.errors import AuthError
from nexus.logging import get_logger

logger = get_logger("nexus.auth")

_security: HTTPBearer = HTTPBearer(auto_error=False)
_uc_sdk_ready: bool = False
_TOKEN_CACHE_TTL: int = 60
_TOKEN_CACHE_MAXSIZE: int = 500


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthDependencies:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._sdk: Optional[object] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ready: bool = False
        self._public_paths: set[str] = set()
        self._public_prefixes: list[str] = []
        self._local_user_sync: Optional[Callable[[dict[str, object]], Awaitable[None]]] = None
        self._token_cache: TTLCache = TTLCache(maxsize=_TOKEN_CACHE_MAXSIZE, ttl=_TOKEN_CACHE_TTL)

    def add_public_path(self, path: str) -> None:
        self._public_paths.add(path)

    def add_public_prefix(self, prefix: str) -> None:
        self._public_prefixes.append(prefix)

    def set_local_user_sync(
        self, func: Callable[[dict[str, object]], Awaitable[None]]
    ) -> None:
        self._local_user_sync = func

    def is_public(self, path: str) -> bool:
        if path in self._public_paths:
            return True
        for prefix in self._public_prefixes:
            if path.startswith(prefix):
                return True
        return False

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
                try:
                    from uc_sdk import UserCenterSDK

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
                await self._bootstrap_sdk(self._sdk)
            return self._sdk if self._ready else None

    async def _bootstrap_sdk(self, sdk: object) -> None:
        uc_cfg = self._config.uc
        if not uc_cfg.app_key or not uc_cfg.app_secret:
            self._ready = True
            logger.info("UC SDK ready without bootstrap (no app_key/app_secret)")
            return
        try:
            ok: bool = await sdk.bootstrap()
            if ok:
                logger.info("UC SDK service token bootstrap success")
            else:
                logger.warning("UC SDK bootstrap failed, verify_token will use local/remote verification")
        except Exception as exc:
            logger.warning("UC SDK bootstrap error: %s, verify_token will use local/remote verification", str(exc))
        self._ready = True

    async def validate_token(self, token: str) -> Optional[dict[str, object]]:
        token_key: str = _hash_token(token)
        cached: Optional[dict[str, object]] = self._token_cache.get(token_key)
        if cached is not None:
            user_id_raw: object = cached.get("user_id")
            if user_id_raw is not None:
                set_request_context(user_id=str(user_id_raw))
            if self._local_user_sync:
                try:
                    await self._local_user_sync(cached)
                except Exception as exc:
                    logger.warning("Local user sync failed: %s", str(exc))
            return cached

        sdk: Optional[object] = await self.get_sdk()
        if sdk is None:
            return None
        try:
            result: dict[str, object] = await sdk.verify_token(token)
            if result and result.get("success", True) is not False:
                user_id_raw: object = result.get("user_id")
                if user_id_raw is not None:
                    set_request_context(user_id=str(user_id_raw))
                if self._local_user_sync:
                    try:
                        await self._local_user_sync(result)
                    except Exception as exc:
                        logger.warning("Local user sync failed: %s", str(exc))
                self._token_cache[token_key] = result
                return result
            return None
        except Exception as exc:
            logger.warning("Token validation failed: %s", str(exc))
            return None

    def invalidate_token_cache(self, token: Optional[str] = None) -> None:
        if token is None:
            self._token_cache.clear()
        else:
            self._token_cache.pop(_hash_token(token), None)

    async def get_user_id_required(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
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
        user_id_raw: object = result.get("user_id")
        if not user_id_raw:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return str(user_id_raw)

    async def get_user_id_optional(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> Optional[str]:
        if credentials is None:
            return None
        result: Optional[dict[str, object]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            return None
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
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> str:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_id_required(credentials)


async def get_current_user_id_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> Optional[str]:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_id_optional(credentials)


async def get_current_user_full(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> dict[str, object]:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_full(credentials)


def normalize_user_dict(user: dict[str, object]) -> dict[str, object]:
    return {
        "user_id": str(user.get("user_id", "")),
        "app_id": user.get("app_id"),
        "role": user.get("role", "user"),
        "vip_level": user.get("vip_level", 0),
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


async def get_current_user_id_int(
    user_id: str = Depends(get_current_user_id_required),
) -> int:
    return parse_user_id(user_id)
