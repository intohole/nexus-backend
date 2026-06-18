from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexus.config import NexusConfig, get_settings
from nexus.context import set_request_context
from nexus.errors import AuthError
from nexus.logging import get_logger

logger = get_logger("nexus.auth")

_security: HTTPBearer = HTTPBearer(auto_error=False)
_uc_sdk_instance: Optional[Any] = None
_uc_sdk_lock: asyncio.Lock = asyncio.Lock()
_uc_sdk_ready: bool = False


class AuthDependencies:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._sdk: Optional[Any] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ready: bool = False
        self._public_paths: set[str] = set()
        self._public_prefixes: list[str] = []
        self._local_user_sync: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None

    def add_public_path(self, path: str) -> None:
        self._public_paths.add(path)

    def add_public_prefix(self, prefix: str) -> None:
        self._public_prefixes.append(prefix)

    def set_local_user_sync(
        self, func: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._local_user_sync = func

    def is_public(self, path: str) -> bool:
        if path in self._public_paths:
            return True
        for prefix in self._public_prefixes:
            if path.startswith(prefix):
                return True
        return False

    async def get_sdk(self) -> Any:
        if self._sdk is not None and self._ready:
            return self._sdk
        async with self._lock:
            if self._sdk is not None and self._ready:
                return self._sdk
            if self._sdk is None:
                try:
                    from usercenter.sdk.python.uc_sdk.client import UserCenterSDK

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

    async def _bootstrap_sdk(self, sdk: Any) -> None:
        try:
            ok: bool = await sdk.bootstrap()
            if ok:
                self._ready = True
                logger.info("UC SDK service token bootstrap success")
            else:
                logger.warning("UC SDK service token bootstrap failed")
        except Exception as exc:
            logger.warning("UC SDK bootstrap error: %s", str(exc))

    async def validate_token(self, token: str) -> Optional[dict[str, Any]]:
        sdk: Optional[Any] = await self.get_sdk()
        if sdk is None:
            return None
        try:
            result: dict[str, Any] = await sdk.validate_token(token)
            if result:
                user_id: Optional[str] = result.get("user_id")
                if user_id:
                    set_request_context(user_id=str(user_id))
                if self._local_user_sync:
                    try:
                        await self._local_user_sync(result)
                    except Exception as exc:
                        logger.warning("Local user sync failed: %s", str(exc))
            return result
        except Exception as exc:
            logger.warning("Token validation failed: %s", str(exc))
            return None

    async def get_user_id_required(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> str:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        result: Optional[dict[str, Any]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        user_id: Optional[str] = result.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return user_id

    async def get_user_id_optional(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> Optional[str]:
        if credentials is None:
            return None
        result: Optional[dict[str, Any]] = await self.validate_token(
            credentials.credentials
        )
        if result is None:
            return None
        return result.get("user_id")

    async def get_user_full(
        self,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> dict[str, Any]:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        result: Optional[dict[str, Any]] = await self.validate_token(
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
) -> dict[str, Any]:
    deps: AuthDependencies = get_auth_deps()
    return await deps.get_user_full(credentials)
