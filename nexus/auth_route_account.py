"""认证账户操作端点组装器：密码族与资料更新，由 auth_routes.create_auth_router 编排。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials

from nexus.auth_models import (
    BindContactRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    SendBindCodeRequest,
    UpdateUserRequest,
)
from nexus.auth_routes import _AuthCtx, _map_uc_detail, _require_auth, _security
from nexus.logging import get_logger

logger = get_logger("nexus.auth_route_account")


def add_password_endpoints(router: APIRouter, ctx: _AuthCtx) -> None:
    @router.post("/change-password")
    async def change_password(
        request: ChangePasswordRequest,
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().change_password(
                request.old_password, request.new_password,
                revoke_others=request.revoke_others, token=credentials.credentials,
            )
            if result.get("success"):
                return ctx.ok(None, "密码修改成功")
            return ctx.err(_map_uc_detail(result, "修改密码失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.post("/forgot-password")
    async def forgot_password(request: ForgotPasswordRequest) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().forgot_password(
                email=request.email, phone=request.phone,
            )
            if result.get("success"):
                return ctx.ok(None, "验证码已发送，请查收")
            return ctx.err(_map_uc_detail(result, "发送失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.post("/reset-password")
    async def reset_password(request: ResetPasswordRequest) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().reset_password(
                request.code, request.new_password,
                email=request.email, phone=request.phone,
            )
            if result.get("success"):
                return ctx.ok(None, "密码已重置，请使用新密码登录")
            return ctx.err(_map_uc_detail(result, "重置失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.post("/send-bind-code")
    async def send_bind_code(
        request: SendBindCodeRequest,
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().send_bind_code(
                email=request.email, phone=request.phone, token=credentials.credentials,
            )
            if result.get("success"):
                return ctx.ok(None, "验证码已发送，请查收")
            return ctx.err(_map_uc_detail(result, "发送失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.put("/bind-contact")
    async def bind_contact(
        request: BindContactRequest,
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().bind_contact(
                request.code, email=request.email, phone=request.phone,
                token=credentials.credentials,
            )
            if result.get("success"):
                return ctx.ok(None, "绑定成功")
            return ctx.err(_map_uc_detail(result, "绑定失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.get("/sessions")
    async def list_sessions(
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().get_sessions(
                token=credentials.credentials
            )
            if result.get("success"):
                return ctx.ok(result.get("data") or [], "获取成功")
            return ctx.err(_map_uc_detail(result, "获取失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.delete("/sessions/{session_id}")
    async def revoke_session(
        session_id: int,
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().revoke_session(
                session_id, token=credentials.credentials
            )
            if result.get("success"):
                return ctx.ok(None, "已下线该设备")
            return ctx.err(_map_uc_detail(result, "操作失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)

    @router.delete("/sessions")
    async def revoke_all_sessions(
        credentials: HTTPAuthorizationCredentials = Depends(_require_auth),
    ) -> object:
        try:
            result: dict[str, object] = await ctx.sdk().revoke_all_sessions(
                token=credentials.credentials
            )
            if result.get("success"):
                return ctx.ok(result.get("data") or None, "所有设备已下线")
            return ctx.err(_map_uc_detail(result, "操作失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)


def add_profile_endpoints(router: APIRouter, ctx: _AuthCtx) -> None:
    @router.put("/me")
    async def update_current_user(
        request: UpdateUserRequest,
        credentials: HTTPAuthorizationCredentials = Depends(_security),
    ) -> object:
        update_data: dict[str, object] = {}
        if request.nickname:
            update_data["nickname"] = request.nickname
        if request.email:
            update_data["email"] = request.email
        if request.phone:
            update_data["phone"] = request.phone
        if request.new_password:
            if not request.old_password:
                return ctx.err("修改密码时必须提供原密码", 400)
            update_data["old_password"] = request.old_password
            update_data["new_password"] = request.new_password
        try:
            result: dict[str, object] = await ctx.sdk().update_current_user(
                update_data, token=credentials.credentials
            )
            if result.get("success"):
                return ctx.ok(result.get("data"), "更新成功")
            return ctx.err(_map_uc_detail(result, "更新失败"), 400)
        except Exception as exc:
            return ctx.handle(exc)
