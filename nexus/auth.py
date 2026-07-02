from __future__ import annotations

import asyncio
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
        if not uc_cfg.app_key:
            self._ready = True
            logger.info("UC SDK ready without bootstrap (no app_key, remote verification only)")
            return
        try:
            ok: bool = await sdk.bootstrap()
            if ok:
                self._ready = True
                logger.info("UC SDK service token bootstrap success")
            else:
                logger.warning("UC SDK service token bootstrap failed")
        except Exception as exc:
            logger.warning("UC SDK bootstrap error: %s", str(exc))

    async def validate_token(self, token: str) -> Optional[dict[str, object]]:
        cached: Optional[dict[str, object]] = self._token_cache.get(token)
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
            if result:
                user_id_raw: object = result.get("user_id")
                if user_id_raw is not None:
                    set_request_context(user_id=str(user_id_raw))
                if self._local_user_sync:
                    try:
                        await self._local_user_sync(result)
                    except Exception as exc:
                        logger.warning("Local user sync failed: %s", str(exc))
                self._token_cache[token] = result
            return result
        except Exception as exc:
            logger.warning("Token validation failed: %s", str(exc))
            return None

    def invalidate_token_cache(self, token: Optional[str] = None) -> None:
        if token is None:
            self._token_cache.clear()
        else:
            self._token_cache.pop(token, None)

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
