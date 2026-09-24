"""权限依赖：用户令牌与 API Key 两类鉴权依赖装配。"""
from __future__ import annotations

from typing import Optional

from cachetools import TTLCache

from nexus.logging import get_logger

logger = get_logger("nexus.permissions")


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
        from nexus.auth import get_auth_deps
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
        from nexus.auth import get_auth_deps
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
        from nexus.auth import get_auth_deps
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


__all__ = [
    "PermissionDependencies",
    "get_permission_deps",
    "ApiKeyDependencies",
    "get_api_key_deps",
]