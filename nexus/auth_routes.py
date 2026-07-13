from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator, model_validator

from nexus.auth import get_current_user_full
from nexus.logging import get_logger

logger = get_logger("nexus.auth_routes")
_security: HTTPBearer = HTTPBearer(auto_error=False)

UcSdkProvider = Callable[[], Any]
OkWrapper = Callable[[Any, str], Any]
ErrWrapper = Callable[[str, int], Any]


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=8, max_length=128)
    email: Optional[str] = None
    phone: Optional[str] = None

    @field_validator("phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., description="刷新令牌")


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateUserRequest(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None
    old_password: Optional[str] = None
    new_password: Optional[str] = Field(None, min_length=8, max_length=128)

    @field_validator("phone", mode="before")
    @classmethod
    def _empty_phone_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _validate_passwords(self) -> "UpdateUserRequest":
        if self.new_password and not self.old_password:
            raise ValueError("修改密码时必须提供旧密码")
        return self


def _default_ok(data: Any, message: str = "") -> Any:
    return data


def _default_err(message: str, status_code: int) -> Any:
    raise HTTPException(status_code=status_code, detail=message)


def _map_uc_detail(result: dict[str, Any], default_msg: str) -> str:
    detail: Any = result.get("detail", result.get("message", default_msg))
    if isinstance(detail, dict):
        return str(detail.get("message", detail.get("detail", default_msg)))
    return str(detail)


def _translate_exception(exc: Exception) -> Any:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, httpx.ConnectError):
        raise HTTPException(status_code=502, detail="认证服务暂时不可用，请稍后重试")
    if isinstance(exc, httpx.TimeoutException):
        raise HTTPException(status_code=502, detail="认证服务响应超时，请稍后重试")
    raise HTTPException(status_code=502, detail=f"认证服务异常: {exc}")


def create_auth_router(
    prefix: str,
    uc_sdk_provider: UcSdkProvider,
    *,
    tags: Optional[list[str]] = None,
    ok: Optional[OkWrapper] = None,
    err: Optional[ErrWrapper] = None,
    include_profile_endpoints: bool = False,
    app_title: str = "",
    app_subtitle: str = "",
) -> APIRouter:
    """创建统一认证路由。

    Args:
        prefix: 路由前缀，如 "/uc-auth" "/auth" "/api/v1/auth"
        uc_sdk_provider: 返回 UC SDK 实例的可调用对象
        ok: 成功响应包装器 (data, message) -> response
        err: 错误响应处理器 (message, status_code) -> response or raise
        include_profile_endpoints: 是否包含 change-password 和 PUT /me
        app_title: 登录页配置中的应用名
        app_subtitle: 登录页配置中的副标题
    """
    if uc_sdk_provider is None:
        raise ValueError("uc_sdk_provider is required")

    wrap_ok: OkWrapper = ok or _default_ok
    wrap_err: ErrWrapper = err or _default_err
    router = APIRouter(prefix=prefix, tags=tags or ["Auth"])

    def _handle(exc: Exception) -> Any:
        if isinstance(exc, HTTPException):
            return wrap_err(str(exc.detail), exc.status_code)
        if isinstance(exc, httpx.ConnectError):
            return wrap_err("认证服务暂时不可用，请稍后重试", 502)
        if isinstance(exc, httpx.TimeoutException):
            return wrap_err("认证服务响应超时，请稍后重试", 502)
        return wrap_err(f"认证服务异常: {exc}", 502)

    @router.post("/login")
    async def login(request: LoginRequest) -> Any:
        try:
            result: dict[str, Any] = await uc_sdk_provider().login(
                username=request.username, password=request.password
            )
            if not result.get("success"):
                return wrap_err(_map_uc_detail(result, "登录失败"), 401)
            data: dict[str, Any] = result.get("data", {})
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

    @router.post("/register")
    async def register(request: RegisterRequest) -> Any:
        try:
            result: dict[str, Any] = await uc_sdk_provider().register(
                username=request.username,
                password=request.password,
                email=request.email or "",
                phone=request.phone or "",
            )
            if not result.get("success"):
                return wrap_err(_map_uc_detail(result, "注册失败"), 400)
            data: dict[str, Any] = result.get("data", {})
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

    @router.post("/refresh")
    async def refresh_token(request: RefreshTokenRequest) -> Any:
        try:
            result: dict[str, Any] = await uc_sdk_provider().refresh_with_token(
                request.refresh_token
            )
            if not result:
                return wrap_err("令牌刷新失败，请重新登录", 401)
            return wrap_ok(result, "刷新成功")
        except Exception as exc:
            return _handle(exc)

    @router.get("/me")
    async def get_me(
        user_info: dict[str, Any] = Depends(get_current_user_full),
        credentials: HTTPAuthorizationCredentials = Depends(_security),
    ) -> Any:
        user_id: Any = user_info.get("user_id")
        username: str = str(user_id) if user_id else ""
        try:
            uc_resp: dict[str, Any] = await uc_sdk_provider().get_current_user(
                token=credentials.credentials
            )
            if isinstance(uc_resp, dict) and uc_resp.get("success"):
                uc_user: dict[str, Any] = uc_resp.get("data") or {}
                username = str(uc_user.get("username") or uc_user.get("id") or user_id)
        except Exception as exc:
            logger.warning("获取用户名失败(user_id=%s): %s", user_id, exc)
        return wrap_ok(
            {
                "id": user_id,
                "username": username,
                "role": user_info.get("role", "user"),
                "vip_level": user_info.get("vip_level", 0),
            },
            "获取成功",
        )

    @router.post("/logout")
    async def logout(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> Any:
        try:
            await uc_sdk_provider().logout(token=credentials.credentials)
        except Exception:
            pass
        return wrap_ok({"success": True}, "登出成功")

    @router.get("/config")
    async def uc_config() -> Any:
        sdk: Any = uc_sdk_provider()
        configured: bool = bool(getattr(sdk, "is_configured", lambda: False)())
        return wrap_ok(
            {
                "enabled": configured,
                "base_url": getattr(sdk, "base_url", None) if configured else None,
                "app_key": getattr(sdk, "app_key", None) if configured else None,
            },
            "获取成功",
        )

    if include_profile_endpoints:

        @router.post("/change-password")
        async def change_password(
            request: ChangePasswordRequest,
            credentials: HTTPAuthorizationCredentials = Depends(_security),
        ) -> Any:
            try:
                result: dict[str, Any] = await uc_sdk_provider().update_current_user(
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
        ) -> Any:
            update_data: dict[str, Any] = {}
            if request.email:
                update_data["email"] = request.email
            if request.phone:
                update_data["phone"] = request.phone
            if request.new_password:
                update_data["old_password"] = request.old_password
                update_data["new_password"] = request.new_password
            try:
                result: dict[str, Any] = await uc_sdk_provider().update_current_user(
                    update_data, token=credentials.credentials
                )
                if result.get("success"):
                    return wrap_ok(result.get("data"), "更新成功")
                return wrap_err(_map_uc_detail(result, "更新失败"), 400)
            except Exception as exc:
                return _handle(exc)

    @router.get("/login-page-config")
    async def login_page_config() -> Any:
        try:
            result: dict[str, Any] = await uc_sdk_provider().get_login_page_config()
            data: dict[str, Any] = result.get("data") or {}
            if app_title:
                data["title"] = app_title
            if app_subtitle:
                data["subtitle"] = data.get("subtitle") or app_subtitle
            return wrap_ok(data, "获取成功")
        except Exception as exc:
            return _handle(exc)

    return router
