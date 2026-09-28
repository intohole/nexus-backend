"""UC 登录态 → 本地影子用户 的 FastAPI 依赖工厂。

收编各业务仓库重复的「HTTPBearer → validate_token → UC get_current_user →
影子用户 find-or-create → get_current_user/get_optional_user/get_current_user_id 三件套」样板。
业务只需提供 ORM 会话依赖与三个小适配器（查用户/建用户/回写更新），鉴权与缓存骨架由本模块统一承担。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.auth import get_auth_deps
from nexus.uc_sdk_helper import get_uc_sdk as _current_uc_sdk

logger = logging.getLogger("nexus.user_auth")

security = HTTPBearer(auto_error=False)

UcUserFetcher = Callable[[str], Awaitable[dict]]
UserFinder = Callable[[AsyncSession, int], Awaitable[Any]]
UserCreator = Callable[[AsyncSession, int, dict], Awaitable[Any]]
UserUpdater = Callable[[AsyncSession, Any, dict], Awaitable[bool]]


async def _fetch_uc_user(token: str) -> dict:
    sdk = _current_uc_sdk()
    result = await sdk.get_current_user(token=token)
    if not isinstance(result, dict) or not result.get("success"):
        return {}
    return result.get("data") or {}


@dataclass
class UserAuthDeps:
    """create_user_auth 的产物：可直接用于 Depends 的三件套 + 底层能力。"""

    get_current_user: Callable
    get_optional_user: Callable
    get_current_user_id: Callable
    ensure_local_user: Callable


def create_user_auth(
    *,
    get_db: Callable,
    find_by_uc_id: UserFinder,
    create_from_uc: UserCreator,
    apply_uc_update: Optional[UserUpdater] = None,
    cache_size: int = 500,
    cache_ttl: float = 60.0,
    unauthorized_detail: str = "无法验证凭据",
) -> UserAuthDeps:
    """生成 UC 登录态依赖三件套。

    find_by_uc_id(db, uc_user_id) -> User | None
    create_from_uc(db, uc_user_id, uc_user) -> User（首次登录建影子用户，可附带建档副作用）
    apply_uc_update(db, user, uc_user) -> bool（可选：登录时回写昵称/头像等，返回是否变更）
    """
    _cache: TTLCache = TTLCache(maxsize=cache_size, ttl=cache_ttl)

    async def _validate(token: str) -> dict:
        deps = get_auth_deps()
        result = await deps.validate_token(token)
        if not result or not result.get("user_id"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=unauthorized_detail,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return dict(result)

    async def ensure_local_user(
        db: AsyncSession,
        uc_info: dict,
        token: Optional[str] = None,
    ) -> Any:
        uc_user_id = int(uc_info["user_id"])
        user = await find_by_uc_id(db, uc_user_id)
        if user is None:
            return await create_from_uc(db, uc_user_id, uc_info)

        if apply_uc_update is not None:
            try:
                if await apply_uc_update(db, user, uc_info):
                    await db.refresh(user)
            except Exception as exc:
                logger.warning("影子用户回写失败 uc_user_id=%s: %s", uc_user_id, exc)
        return user

    async def _resolve(
        credentials: Optional[HTTPAuthorizationCredentials],
        db: AsyncSession,
    ) -> Any:
        if not credentials or not credentials.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=unauthorized_detail,
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = credentials.credentials
        uc_info: Optional[dict] = _cache.get(token)
        if uc_info is None:
            uc_info = await _validate(token)
            if not uc_info.get("username"):
                fetched = await _fetch_uc_user(token)
                if fetched:
                    uc_info = {**uc_info, **fetched}
            _cache[token] = uc_info
        return await ensure_local_user(db, uc_info)

    async def get_current_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        return await _resolve(credentials, db)

    async def get_optional_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
    ) -> Any:
        if not credentials or not credentials.credentials:
            return None
        try:
            return await _resolve(credentials, db)
        except HTTPException:
            return None

    async def get_current_user_id(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
    ) -> int:
        user = await _resolve(credentials, db)
        return user.id

    return UserAuthDeps(
        get_current_user=get_current_user,
        get_optional_user=get_optional_user,
        get_current_user_id=get_current_user_id,
        ensure_local_user=ensure_local_user,
    )
