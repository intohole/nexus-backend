"""UC 登录态 → 本地影子用户 的 FastAPI 依赖工厂。

收编各业务仓库重复的「HTTPBearer → validate_token → UC get_current_user →
影子用户 find-or-create → get_current_user/get_optional_user/get_current_user_id 三件套」样板。
业务只需提供 ORM 会话依赖与三个小适配器（查用户/建用户/回写更新），鉴权与缓存骨架由本模块统一承担。

服务委托（oneNote r18）：服务身份（app_N）+ X-On-Behalf-Of = 平台代表用户调用，
与 nexus.auth get_user_id_required 同语义同源（DELEGATION_HEADER/_DELEGATED_ID_RE/
_is_service_principal 单源自 auth.py）；无委托头的服务凭证保持 fail-closed 401
（oneNote r15 判例）。此前本模块不消费委托头，与 auth.py 行为漂移——nexus-agent
晨报/routine 在用户 token 过期后走委托通道，auth.py 形态应用（gezhi）正常、
本模块形态应用（oneNote 等 5 仓）结构性 401（11-01/11-02 拾光段两连缺席实锤）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Optional, TypeVar

from cachetools import TTLCache
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.auth import DELEGATION_HEADER, AuthDependencies, _DELEGATED_ID_RE, get_auth_deps
from nexus.context import set_request_context

logger = logging.getLogger("nexus.user_auth")

security = HTTPBearer(auto_error=False)

UserT = TypeVar("UserT")

UserFinder = Callable[[AsyncSession, int], Awaitable[Optional[UserT]]]
UserCreator = Callable[[AsyncSession, int, dict], Awaitable[UserT]]
UserUpdater = Callable[[AsyncSession, UserT, dict], Awaitable[bool]]


async def get_bearer_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[str]:
    """提取 Bearer token（未携带返回 None）。收编各业务仓逐字重复的样板依赖。"""
    if not credentials or not credentials.credentials:
        return None
    return credentials.credentials


async def _fetch_uc_user(token: str) -> dict:
    try:
        sdk = await get_auth_deps().get_sdk()
    except Exception as exc:
        logger.warning("UC SDK 解析失败，跳过用户信息补全: %s", exc)
        return {}
    if sdk is None:
        logger.warning("UC SDK 未就绪，跳过用户信息补全")
        return {}
    try:
        result = await sdk.get_current_user(token=token)
    except Exception as exc:
        logger.warning("UC 用户信息补全失败: %s", exc)
        return {}
    if not isinstance(result, dict) or not result.get("success"):
        return {}
    return result.get("data") or {}


@dataclass
class UserAuthDeps(Generic[UserT]):
    """create_user_auth 的产物：可直接用于 Depends 的三件套 + 底层能力。"""

    get_current_user: Callable[..., Awaitable[UserT]]
    get_optional_user: Callable[..., Awaitable[Optional[UserT]]]
    get_current_user_id: Callable[..., Awaitable[int]]
    ensure_local_user: Callable[..., Awaitable[UserT]]


def create_user_auth(
    *,
    get_db: Callable,
    find_by_uc_id: UserFinder,
    create_from_uc: UserCreator,
    apply_uc_update: Optional[UserUpdater] = None,
    cache_size: int = 500,
    cache_ttl: float = 60.0,
    unauthorized_detail: str = "无法验证凭据",
) -> UserAuthDeps[UserT]:
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
        uc_info: dict[str, object],
        token: Optional[str] = None,
    ) -> UserT:
        try:
            uc_user_id = int(uc_info.get("user_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            # 服务身份（app_N 等）拿着有效令牌访问用户端点：不是用户令牌，按未认证处理，
            # 不能让 int() 的 ValueError 漏成 400「invalid literal」噪音（oneNote r15 生产实锤）。
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=unauthorized_detail,
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
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
        request: Optional[Request] = None,
    ) -> UserT:
        if not credentials or not credentials.credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=unauthorized_detail,
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = credentials.credentials
        uc_info: Optional[dict[str, object]] = _cache.get(token)
        if uc_info is None:
            uc_info = await _validate(token)
            if not uc_info.get("username"):
                fetched = await _fetch_uc_user(token)
                if fetched:
                    uc_info = {**uc_info, **fetched}
            _cache[token] = uc_info
        uc_info = dict(uc_info)
        # 服务委托解析（与 nexus.auth get_user_id_required 同语义）：验签通过的服务身份
        # + 合法 X-On-Behalf-Of → 代表该用户；委托改写只发生在本地副本，绝不写回 token
        # 缓存（同一服务 token 可代表不同用户）。非法委托头显式 403（可诊断）；无委托头
        # 落到 ensure_local_user 的服务身份守卫=401（oneNote r15 判例 fail-closed 保持）。
        if request is not None and AuthDependencies._is_service_principal(uc_info):
            header = (request.headers.get(DELEGATION_HEADER) or "").strip()
            if header:
                if not _DELEGATED_ID_RE.fullmatch(header):
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="invalid X-On-Behalf-Of",
                    )
                uc_info["user_id"] = header
                set_request_context(user_id=header)
        return await ensure_local_user(db, uc_info)

    async def get_current_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
        request: Request = None,  # 裸 Request 注解：FastAPI 才会自动注入（Optional[Request] 会被当 Pydantic 字段建模，启动即炸）
    ) -> UserT:
        return await _resolve(credentials, db, request)

    async def get_optional_user(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
        request: Request = None,
    ) -> Optional[UserT]:
        if not credentials or not credentials.credentials:
            return None
        try:
            return await _resolve(credentials, db, request)
        except HTTPException:
            return None

    async def get_current_user_id(
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
        db: AsyncSession = Depends(get_db),
        request: Request = None,
    ) -> int:
        user = await _resolve(credentials, db, request)
        return user.id

    return UserAuthDeps(
        get_current_user=get_current_user,
        get_optional_user=get_optional_user,
        get_current_user_id=get_current_user_id,
        ensure_local_user=ensure_local_user,
    )
