"""认证核心会话端点组装器：登录/注册/刷新/会话信息/公开配置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials

from nexus.auth import get_current_user_full
from nexus.auth_models import LoginRequest, RefreshTokenRequest, RegisterRequest
from nexus.auth_routes import _AuthCtx, _map_uc_detail, _security
from nexus.logging import get_logger

logger = get_logger("nexus.auth_route_endpoints")


def add_core_endpoints(router: APIRouter, ctx: _AuthCtx, eps: set[str]) -> None:
    if "login" in eps:

        @router.post("/login")
        async def login(request: LoginRequest) -> object:
            try:
                login_kwargs = (
                    {"phone": request.phone, "password": request.password}
                    if request.login_type == "phone"
                    else {"username": request.username, "password": request.password}
                )
                result: dict[str, object] = await ctx.sdk().login(**login_kwargs)
                if not result.get("success"):
                    return ctx.err(
                        _map_uc_detail(result, "登录失败"),
                        int(result.get("status_code") or 401),
                    )
                data: dict[str, object] = result.get("data", {})
                if ctx.post_login_hook:
                    try:
                        await ctx.post_login_hook(data)
                    except Exception as exc:
                        logger.warning("post_login_hook failed: %s", exc)
                return ctx.ok(
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
                return ctx.handle(exc)

    if "register" in eps:

        @router.post("/register")
        async def register(request: RegisterRequest) -> object:
            try:
                result: dict[str, object] = await ctx.sdk().register(
                    username=request.username,
                    password=request.password,
                    email=request.email or "",
                    phone=request.phone or "",
                )
                if not result.get("success"):
                    return ctx.err(
                        _map_uc_detail(result, "注册失败"),
                        int(result.get("status_code") or 400),
                    )
                data: dict[str, object] = result.get("data", {})
                if ctx.post_register_hook:
                    try:
                        await ctx.post_register_hook(data)
                    except Exception as exc:
                        logger.warning("post_register_hook failed: %s", exc)
                return ctx.ok(
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
                return ctx.handle(exc)

    if "refresh" in eps:

        @router.post("/refresh")
        async def refresh_token(request: RefreshTokenRequest) -> object:
            try:
                result: dict[str, object] = await ctx.sdk().refresh_with_token(
                    request.refresh_token
                )
                if not result or not result.get("access_token"):
                    return ctx.err(
                        _map_uc_detail(result or {}, "令牌刷新失败，请重新登录"), 401
                    )
                return ctx.ok(
                    {
                        "access_token": result.get("access_token"),
                        "refresh_token": result.get("refresh_token"),
                        "token_type": result.get("token_type", "bearer"),
                        "expires_in": result.get("expires_in"),
                    },
                    "刷新成功",
                )
            except Exception as exc:
                return ctx.handle(exc)

    if "me" in eps:

        @router.get("/me")
        async def get_me(
            user_info: dict[str, object] = Depends(get_current_user_full),
            credentials: HTTPAuthorizationCredentials = Depends(_security),
        ) -> object:
            user_id: object = user_info.get("user_id")
            username: str = ""
            nickname: str = ""
            display_name: str = ""
            uc_user: dict[str, object] = {}
            try:
                uc_resp: dict[str, object] = await ctx.sdk().get_current_user(
                    token=credentials.credentials
                )
                if isinstance(uc_resp, dict) and uc_resp.get("success"):
                    uc_user = uc_resp.get("data") or {}
                    username = str(uc_user.get("username") or "")
                    nickname = str(uc_user.get("nickname") or "")
                    display_name = str(uc_user.get("display_name") or "")
            except Exception as exc:
                logger.warning("获取用户信息失败(user_id=%s): %s", user_id, exc)
            if ctx.me_transformer:
                try:
                    user_id_str = str(user_id) if user_id else ""
                    enriched: dict[str, object] = dict(user_info)
                    for key in ("username", "nickname", "display_name", "email", "phone"):
                        if uc_user.get(key):
                            enriched[key] = uc_user[key]
                    transformed = await ctx.me_transformer(enriched, user_id_str)
                    return ctx.ok(transformed, "获取成功")
                except Exception as exc:
                    logger.warning("me_transformer failed: %s", exc)
            return ctx.ok(
                {
                    "id": user_id,
                    "username": username,
                    "nickname": nickname,
                    "display_name": display_name,
                    "role": user_info.get("role", "user"),
                    "vip_level": user_info.get("vip_level", 0),
                },
                "获取成功",
            )

    if "logout" in eps:

        @router.post("/logout")
        async def logout(credentials: HTTPAuthorizationCredentials = Depends(_security)) -> object:
            try:
                await ctx.sdk().logout(token=credentials.credentials)
            except Exception as exc:
                logger.warning("UC logout failed (client-side logout still succeeds): %s", exc)
            return ctx.ok({"success": True}, "登出成功")


def add_config_endpoints(router: APIRouter, ctx: _AuthCtx, eps: set[str]) -> None:
    if "config" in eps:

        @router.get("/config")
        async def uc_config() -> object:
            sdk: object = ctx.sdk()
            configured: bool = bool(getattr(sdk, "is_configured", lambda: False)())
            app_key: str = getattr(sdk, "app_key", "") or ""
            return ctx.ok(
                {
                    "enabled": configured,
                    "base_url": ctx.frontend_uc_url if configured else "",
                    "app_key": app_key if configured else "",
                },
                "获取成功",
            )

    if "login-page-config" in eps:

        @router.get("/login-page-config")
        async def login_page_config() -> object:
            try:
                result: dict[str, object] = await ctx.sdk().get_login_page_config()
                data: dict[str, object] = result.get("data") or {}
                if ctx.app_title:
                    data["title"] = ctx.app_title
                if ctx.app_subtitle:
                    data["subtitle"] = data.get("subtitle") or ctx.app_subtitle
                return ctx.ok(data, "获取成功")
            except Exception as exc:
                return ctx.handle(exc)
