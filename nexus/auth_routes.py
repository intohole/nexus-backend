"""认证路由工厂：登录/注册/刷新/改密/找回密码等端点装配（编排层，端点组装见 auth_route_endpoints）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from nexus.auth import get_current_user_full
from nexus.logging import get_logger
from nexus.uc_sdk_helper import standard_err

logger = get_logger("nexus.auth_routes")
_security: HTTPBearer = HTTPBearer(auto_error=False)

UcSdkProvider = Callable[[], object]
OkWrapper = Callable[[object, str], object]
ErrWrapper = Callable[[str, int], object]
PostActionHook = Callable[[dict[str, object]], Awaitable[None]]
MeTransformer = Callable[[dict[str, object], str], Awaitable[dict[str, object]]]

DEFAULT_ENDPOINTS: frozenset[str] = frozenset(
    {"login", "register", "refresh", "me", "logout", "config", "login-page-config"}
)


def _default_ok(data: object, message: str = "") -> object:
    return data


def _map_uc_detail(result: dict[str, object], default_msg: str) -> str:
    detail: object = result.get("detail", result.get("message", default_msg))
    if isinstance(detail, dict):
        return str(detail.get("message", detail.get("detail", default_msg)))
    return str(detail)


async def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> HTTPAuthorizationCredentials:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="请先登录")
    return credentials


@dataclass
class _AuthCtx:
    """端点组装器共享的闭包上下文。"""
    sdk: UcSdkProvider
    ok: OkWrapper
    err: ErrWrapper
    handle: Callable[[Exception], object]
    frontend_uc_url: str
    app_title: str
    app_subtitle: str
    post_login_hook: Optional[PostActionHook]
    post_register_hook: Optional[PostActionHook]
    me_transformer: Optional[MeTransformer]


def _make_handle(wrap_err: ErrWrapper) -> Callable[[Exception], object]:
    def _handle(exc: Exception) -> object:
        if isinstance(exc, HTTPException):
            return wrap_err(str(exc.detail), exc.status_code)
        if isinstance(exc, httpx.ConnectError):
            return wrap_err("认证服务暂时不可用，请稍后重试", 502)
        if isinstance(exc, httpx.TimeoutException):
            return wrap_err("认证服务响应超时，请稍后重试", 502)
        return wrap_err(f"认证服务异常: {exc}", 502)
    return _handle


def create_auth_router(
    prefix: str,
    uc_sdk_provider: UcSdkProvider,
    *,
    tags: Optional[list[str]] = None,
    ok: Optional[OkWrapper] = None,
    err: Optional[ErrWrapper] = None,
    endpoints: Optional[set[str]] = None,
    include_profile_endpoints: bool = False,
    password_ops: bool = False,
    frontend_uc_url: str = "/uc-api",
    app_title: str = "",
    app_subtitle: str = "",
    post_login_hook: Optional[PostActionHook] = None,
    post_register_hook: Optional[PostActionHook] = None,
    me_transformer: Optional[MeTransformer] = None,
) -> APIRouter:
    if uc_sdk_provider is None:
        raise ValueError("uc_sdk_provider is required")
    from nexus.auth_route_account import add_password_endpoints, add_profile_endpoints
    from nexus.auth_route_endpoints import add_config_endpoints, add_core_endpoints

    wrap_ok: OkWrapper = ok or _default_ok
    wrap_err: ErrWrapper = err or standard_err
    eps: set[str] = endpoints or set(DEFAULT_ENDPOINTS)
    router = APIRouter(prefix=prefix, tags=tags or ["Auth"])
    ctx = _AuthCtx(
        sdk=uc_sdk_provider,
        ok=wrap_ok,
        err=wrap_err,
        handle=_make_handle(wrap_err),
        frontend_uc_url=frontend_uc_url,
        app_title=app_title,
        app_subtitle=app_subtitle,
        post_login_hook=post_login_hook,
        post_register_hook=post_register_hook,
        me_transformer=me_transformer,
    )
    add_core_endpoints(router, ctx, eps)
    if password_ops:
        add_password_endpoints(router, ctx)
    if include_profile_endpoints:
        add_profile_endpoints(router, ctx)
    add_config_endpoints(router, ctx, eps)
    return router
