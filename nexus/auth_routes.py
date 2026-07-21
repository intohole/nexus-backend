from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from nexus.auth import get_current_user_full
from nexus.auth_models import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    UpdateUserRequest,
)
from nexus.auth_route_types import (
    DEFAULT_ENDPOINTS,
    ErrWrapper,
    MeTransformer,
    OkWrapper,
    PostActionHook,
    UcSdkProvider,
    default_err as _default_err,
    default_ok as _default_ok,
    map_uc_detail as _map_uc_detail,
)
from nexus.logging import get_logger

logger = get_logger("nexus.auth_routes")
_security: HTTPBearer = HTTPBearer(auto_error=False)


def create_auth_router(
    prefix: str,
    uc_sdk_provider: UcSdkProvider,
    *,
    tags: Optional[list[str]] = None,
    ok: Optional[OkWrapper] = None,
    err: Optional[ErrWrapper] = None,
    endpoints: Optional[set[str]] = None,
    include_profile_endpoints: bool = False,
    app_title: str = "",
    app_subtitle: str = "",
    post_login_hook: Optional[PostActionHook] = None,
    post_register_hook: Optional[PostActionHook] = None,
    me_transformer: Optional[MeTransformer] = None,
) -> APIRouter:
    if uc_sdk_provider is None:
        raise ValueError("uc_sdk_provider is required")
    wrap_ok: OkWrapper = ok or _default_ok
    wrap_err: ErrWrapper = err or _default_err
    eps: set[str] = endpoints or set(DEFAULT_ENDPOINTS)
    router = APIRouter(prefix=prefix, tags=tags or ["Auth"])

    def _handle(exc: Exception) -> object:
        if isinstance(exc, HTTPException):
            return wrap_err(str(exc.detail), exc.status_code)
        if isinstance(exc, httpx.ConnectError):
            return wrap_err("认证服务暂时不可用，请稍后重试", 502)
        if isinstance(exc, httpx.TimeoutException):
            return wrap_err("认证服务响应超时，请稍后重试", 502)
        return wrap_err(f"认证服务异常: {exc}", 502)

    if "login" in eps:

        @router.post("/login")
        async def login(request: LoginRequest) -> object:
            try:
                login_kwargs = (
                    {"phone": request.phone, "password": request.password}
                    if request.login_type == "phone"
                    else {"username": request.username, "password": request.password}
                )
                result: dict[str, object] = await uc_sdk_provider().login(**login_kwargs)
                if not result.get("success"):
                    return wrap_err(_map_uc_detail(result, "登录失败"), 401)
                data: dict[str, object] = result.get("data", {})
                if post_login_hook:
                    try:
                        await post_login_hook(data)
                    except Exception as exc:
                        logger.warning("post_login_hook failed: %s", exc)
                return wrap_ok(
                    {
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token"),
                        "token_type": data.get("token_type", "bearer"),
                        "expires_in": data.get("expires_in"),
                        "user": data.get("user"),
                        "vip_level": data.get("vip_level", 0),
                    },
                    "登录成功",
                )
            except Exception as exc:
                return _handle(exc)

    if "register" in eps:

        @router.post("/register")
        async def register(request: RegisterRequest) -> object:
            try:
                result: dict[str, object] = await uc_sdk_provider().register(
                    username=request.username,
                    password=request.password,
                    email=request.email or "",
                    phone=request.phone or "",
                )
                if not result.get("success"):
                    return wrap_err(_map_uc_detail(result, "注册失败"), 400)
                data: dict[str, object] = result.get("data", {})
                if post_register_hook:
                    try:
                        await post_register_hook(data)
                    except Exception as exc:
                        logger.warning("post_register_hook failed: %s", exc)
                return wrap_ok(
                    {
                        "access_token": data.get("access_token"),
                        "refresh_token": data.get("refresh_token"),
                        "token_type": data.get("token_type", "bearer"),
                        "expires_in": data.get("expires_in"),
                        "user": data.get("user"),
                    },
                    "注册成功",
                )
            except Exception as exc:
                return _handle(exc)

    if "refresh" in eps:

        @router.post("/refresh")
        async def refresh_token(request: RefreshTokenRequest) -> object:
            try:
                result: dict[str, object] = await uc_sdk_provider().refresh_with_token(
                    request.refresh_token
                )
                if not result or not result.get("access_token"):
                    return wrap_err(
                        _map_uc_detail(result or {}, "令牌刷新失败，请重新登录"), 401
                    )
                return wrap_ok(
                    {
                        "access_token": result.get("access_token"),
                        "refresh_token": result.get("refresh_token"),
                        "token_type": result.get("token_type", "bearer"),
                        "expires_in": result.get("expires_in"),
                    },
                    "刷新成功",
                )
            except Exception as exc:
                return _handle(exc)

    if "me" in eps:

        @router.get("/me")
        async def get_me(
            user_info: dict[str, object] = Depends(get_current_user_full),
            credentials: HTTPAuthorizationCredentials = Depends(_security),
        ) -> object:
            user_id: object = user_info.get("user_id")
            username: str = str(user_id) if user_id else ""
            try:
                uc_resp: dict[str, object] = await uc_sdk_provider().get_current_user(
                    token=credentials.credentials
                )
                if isinstance(uc_resp, dict) and uc_resp.get("success"):
                    uc_user: dict[str, object] = uc_resp.get("data") or {}
                    username = str(uc_user.get("username") or uc_user.get("id") or user_id)
            except Exception as exc:
                logger.warning("获取用户名失败(user_id=%s): %s", user_id, exc)
            if me_transformer:
                try:
                    user_id_str = str(user_id) if user_id else ""
                    transformed = await me_transformer(user_info, user_id_str)
                    return wrap_ok(transformed, "获取成功")
                except Exception as exc:
                    logger.warning("me_transformer failed: %s", exc)
            return wrap_ok(
                {
                    "id": user_id,
                    "username": username,
                    "role": user_info.get("role", "user"),
                    "vip_level": user_info.get("vip_level", 0),
                },
                "获取成功",
            )

    if "logout" in eps:

        @router.post("/logout")
        async def logout(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> object:
            try:
                await uc_sdk_provider().logout(token=credentials.credentials)
            except Exception as exc:
                logger.warning("UC logout failed (client-side logout still succeeds): %s", exc)
            return wrap_ok({"success": True}, "登出成功")

    if "config" in eps:

        @router.get("/config")
        async def uc_config() -> object:
            sdk: object = uc_sdk_provider()
            configured: bool = bool(getattr(sdk, "is_configured", lambda: False)())
            return wrap_ok(
                {
                    "enabled": configured,
                    "base_url": getattr(sdk, "base_url", None) if configured else None,
                },
                "获取成功",
            )

    if include_profile_endpoints:

        @router.post("/change-password")
        async def change_password(
            request: ChangePasswordRequest,
            credentials: HTTPAuthorizationCredentials = Depends(_security),
        ) -> object:
            try:
                result: dict[str, object] = await uc_sdk_provider().update_current_user(
                    {"old_password": request.old_password, "new_password": request.new_password},
                    token=credentials.credentials,
                )
                if result.get("success"):
                    return wrap_ok(None, "密码修改成功")
                return wrap_err(_map_uc_detail(result, "修改密码失败"), 400)
            except Exception as exc:
                return _handle(exc)

        @router.put("/me")
        async def update_current_user(
            request: UpdateUserRequest,
            credentials: HTTPAuthorizationCredentials = Depends(_security),
        ) -> object:
            update_data: dict[str, object] = {}
            if request.email:
                update_data["email"] = request.email
            if request.phone:
                update_data["phone"] = request.phone
            if request.new_password:
                if not request.old_password:
                    return wrap_err("修改密码时必须提供原密码", 400)
                update_data["old_password"] = request.old_password
                update_data["new_password"] = request.new_password
            try:
                result: dict[str, object] = await uc_sdk_provider().update_current_user(
                    update_data, token=credentials.credentials
                )
                if result.get("success"):
                    return wrap_ok(result.get("data"), "更新成功")
                return wrap_err(_map_uc_detail(result, "更新失败"), 400)
            except Exception as exc:
                return _handle(exc)

    if "login-page-config" in eps:

        @router.get("/login-page-config")
        async def login_page_config() -> object:
            try:
                result: dict[str, object] = await uc_sdk_provider().get_login_page_config()
                data: dict[str, object] = result.get("data") or {}
                if app_title:
                    data["title"] = app_title
                if app_subtitle:
                    data["subtitle"] = data.get("subtitle") or app_subtitle
                return wrap_ok(data, "获取成功")
            except Exception as exc:
                return _handle(exc)

    return router
