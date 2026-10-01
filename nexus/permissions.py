"""权限依赖：用户令牌与 API Key 两类鉴权依赖装配，含鉴权组合器。

依赖方向：本模块（鉴权 authz）→ nexus.auth（认证 authn），单向向下；
require_permission/require_api_key 等"认证+鉴权"组合器收拢在此，
auth.py 保持纯认证层，不反向 import 本模块。
"""
from __future__ import annotations

from typing import Callable, Optional

from cachetools import TTLCache
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexus.auth import extract_bearer_token, get_auth_deps
from nexus.logging import get_logger

logger = get_logger("nexus.permissions")

_security: HTTPBearer = HTTPBearer(auto_error=False)


async def _hash_token(token: str) -> str:
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


PERM_CACHE_TTL: int = 30
PERM_CACHE_MAXSIZE: int = 10000


class PermissionDependencies:
    def __init__(self) -> None:
        self._perm_cache: TTLCache = TTLCache(maxsize=PERM_CACHE_MAXSIZE, ttl=PERM_CACHE_TTL)

    async def user_has_permission(
        self,
        credentials: object,
        permission_code: str,
    ) -> bool:
        if credentials is None:
            return False
        result = await get_auth_deps().validate_token(
            credentials.credentials
        )
        if not result:
            return False
        role = result.get("role")
        if role == "admin":
            return True
        cache_key = f"{await _hash_token(credentials.credentials)}:{permission_code}"
        cached = self._perm_cache.get(cache_key)
        if cached is not None:
            return bool(cached)
        sdk = await get_auth_deps().get_sdk()
        has = False
        if sdk is not None:
            try:
                perm_result = await sdk.check_permission(
                    credentials.credentials, permission_code
                )
                if perm_result.get("success"):
                    has = bool(perm_result.get("data", {}).get("has_permission", False))
            except Exception as exc:
                logger.warning("Permission check failed: %s", str(exc))
        self._perm_cache[cache_key] = has or False
        return has

    async def get_user_org_id(self, credentials: object) -> Optional[str]:
        if credentials is None:
            return None
        result = await get_auth_deps().validate_token(
            credentials.credentials
        )
        if not result:
            return None
        org_id_raw = result.get("org_id")
        return str(org_id_raw) if org_id_raw is not None else None


_permission_deps: Optional[PermissionDependencies] = None


def get_permission_deps() -> PermissionDependencies:
    global _permission_deps
    if _permission_deps is None:
        _permission_deps = PermissionDependencies()
    return _permission_deps


API_KEY_CACHE_TTL: int = 30
API_KEY_CACHE_MAXSIZE: int = 5000


class ApiKeyDependencies:
    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=API_KEY_CACHE_MAXSIZE, ttl=API_KEY_CACHE_TTL)

    async def verify(self, api_key: str, scope: Optional[str] = None) -> Optional[dict[str, object]]:
        cache_key = f"{await _hash_token(api_key)}:{scope or '*'}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached if cached else None
        sdk = await get_auth_deps().get_sdk()
        if sdk is None:
            return None
        try:
            result = await sdk._request(
                "POST", "/api/internal/api-key/verify",
                json={"api_key": api_key, "scope": scope},
            )
        except Exception as exc:
            logger.warning("API key verify failed: %s", str(exc))
            return None
        data = result.get("data", {}) if result.get("success") else {}
        if not data.get("valid"):
            self._cache[cache_key] = None
            return None
        self._cache[cache_key] = data
        return data


_api_key_deps: Optional[ApiKeyDependencies] = None


def get_api_key_deps() -> ApiKeyDependencies:
    global _api_key_deps
    if _api_key_deps is None:
        _api_key_deps = ApiKeyDependencies()
    return _api_key_deps


def require_permission(permission_code: str) -> Callable:
    """返回 FastAPI 依赖，校验当前用户是否具备指定权限码。

    用法: async def ep(user = Depends(require_permission("adsmart.campaign.manage"))): ...
    """
    async def dependency(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
    ) -> dict[str, object]:
        if credentials is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        user: dict[str, object] = await get_auth_deps().get_user_full(credentials)
        has: bool = await get_permission_deps().user_has_permission(
            credentials, permission_code
        )
        if not has:
            raise HTTPException(status_code=403, detail="无权限执行该操作")
        return user
    return dependency


async def get_current_org_id_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security),
) -> Optional[str]:
    return await get_permission_deps().get_user_org_id(credentials)


def require_api_key(scope: Optional[str] = None) -> Callable:
    """返回 FastAPI 依赖，校验开放 API Key（Authorization: Bearer / X-Api-Key）。

    用法: async def ep(info = Depends(require_api_key("adsmart.campaign.read"))): ...
    """
    async def dependency(
        authorization: Optional[str] = None,
        x_api_key: Optional[str] = None,
    ) -> dict[str, object]:
        api_key: str = x_api_key or extract_bearer_token(authorization or "")
        if not api_key:
            raise HTTPException(status_code=401, detail="缺少 API Key")
        info: Optional[dict[str, object]] = await get_api_key_deps().verify(api_key, scope)
        if not info:
            raise HTTPException(status_code=401, detail="API Key 无效或不具备所需权限")
        return info
    return dependency


__all__ = [
    "PermissionDependencies",
    "get_permission_deps",
    "ApiKeyDependencies",
    "get_api_key_deps",
    "require_permission",
    "get_current_org_id_optional",
    "require_api_key",
]