from typing import Optional, Dict, Any


class UserMixin:
    async def get_current_user(self, token: str = None) -> Dict[str, Any]:
        return await self._request("GET", "/api/users/me", token=token)

    async def update_current_user(self, update_data: Dict[str, Any], token: str = None) -> Dict[str, Any]:
        return await self._request("PUT", "/api/users/me", token=token, json=update_data)

    async def get_user_permissions(self, token: str = None) -> Dict[str, Any]:
        return await self._request("GET", "/api/users/permissions", token=token)

    async def get_users(self, token: str = None, skip: int = 0, limit: int = 100) -> Dict[str, Any]:
        return await self._request("GET", f"/api/users?skip={skip}&limit={limit}", token=token)

    async def get_user(self, token: str, user_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"/api/users/{user_id}", token=token)

    async def update_user(self, token: str, user_id: int, update_data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PUT", f"/api/users/{user_id}", token=token, json=update_data)

    async def delete_user(self, token: str, user_id: int) -> Dict[str, Any]:
        return await self._request("DELETE", f"/api/users/{user_id}", token=token)

    async def get_userinfo(self) -> dict:
        return await self._request("GET", "/api/auth/userinfo")


class AppMixin:
    async def get_applications(self, token: str = None, skip: int = 0, limit: int = 100) -> Dict[str, Any]:
        return await self._request("GET", f"/api/applications?skip={skip}&limit={limit}", token=token)

    async def get_application(self, token: str, app_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"/api/applications/{app_id}", token=token)

    async def create_application(self, token: str, app_data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/api/applications", token=token, json=app_data)

    async def update_application(self, token: str, app_id: int, app_data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PUT", f"/api/applications/{app_id}", token=token, json=app_data)

    async def delete_application(self, token: str, app_id: int) -> Dict[str, Any]:
        return await self._request("DELETE", f"/api/applications/{app_id}", token=token)

    async def get_login_page_config(self, app_key: str = None) -> Dict[str, Any]:
        key = app_key or self.app_key
        return await self._request("GET", f"/api/auth/login-page-config?app_key={key}")


class VipMixin:
    async def get_vip_levels(self) -> dict:
        return await self._request("GET", "/api/vip/levels", params={"app_key": self.app_key})

    async def create_vip_level(self, app_id: int, level_name: str, level_code: str, **kwargs) -> dict:
        data = {"app_id": app_id, "level_name": level_name, "level_code": level_code, **kwargs}
        return await self._request("POST", "/api/vip/levels", json=data)

    async def upgrade_vip(self, level_code: str, duration_days: int = None) -> dict:
        data = {"level_code": level_code}
        if duration_days:
            data["duration_days"] = duration_days
        return await self._request("POST", "/api/vip/upgrade", json=data)

    async def check_vip_expiry(self) -> dict:
        return await self._request("GET", "/api/vip/check-expiry")


class InviteCodeMixin:
    async def validate_invite_code(self, code: str, app_key: str = None) -> Dict[str, Any]:
        return await self._request(
            "POST", "/api/invite-codes/validate",
            json={"code": code, "app_key": app_key or self.app_key}
        )

    async def use_invite_code(self, invite_code: str, token: str = None) -> Dict[str, Any]:
        return await self._request(
            "POST", "/api/invite-codes/use",
            token=token, json={"invite_code": invite_code}
        )

    async def create_invite_code_batch(self, app_id: int, batch_name: str, total_count: int = 10, **kwargs) -> dict:
        data = {"app_id": app_id, "batch_name": batch_name, "total_count": total_count, **kwargs}
        return await self._request("POST", "/api/invite-codes/batch", json=data)

    async def get_invite_code_batches(self, app_id: int = None) -> dict:
        params = {}
        if app_id:
            params["app_id"] = app_id
        return await self._request("GET", "/api/invite-codes/batch", params=params)


class ThirdPartyMixin:
    async def third_party_login(self, provider: str, code: str,
                                 state: str = None, extra: Dict[str, Any] = None) -> Dict[str, Any]:
        data = {"app_key": self.app_key, "provider": provider, "code": code}
        if state:
            data["state"] = state
        if extra:
            data["extra"] = extra
        result = await self._request("POST", "/api/auth/third-party", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def switch_app(self, app_key: str) -> dict:
        result = await self._request(
            "POST", "/api/auth/switch-app",
            json={"refresh_token": self._refresh_token, "app_key": app_key}
        )
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def get_my_apps(self) -> dict:
        return await self._request("GET", "/api/auth/my-apps")


class DiscoveryMixin:
    async def get_discovery(self) -> dict:
        return await self._request("GET", "/api/discovery")

    async def get_integration_guide(self) -> dict:
        return await self._request("GET", "/api/discovery/integration-guide")


class ApiTokenMixin:
    async def create_api_token(self, name: str, scopes: list = None,
                                app_id: int = None, expires_at: str = None) -> dict:
        data = {"name": name}
        if scopes:
            data["scopes"] = scopes
        if app_id is not None:
            data["app_id"] = app_id
        if expires_at:
            data["expires_at"] = expires_at
        return await self._request("POST", "/api/api-tokens", json=data)

    async def list_api_tokens(self, app_id: int = None, status: str = None) -> dict:
        params = {}
        if app_id is not None:
            params["app_id"] = app_id
        if status:
            params["status"] = status
        return await self._request("GET", "/api/api-tokens", params=params)

    async def get_api_token(self, token_id: int) -> dict:
        return await self._request("GET", f"/api/api-tokens/{token_id}")

    async def update_api_token(self, token_id: int, **kwargs) -> dict:
        return await self._request("PUT", f"/api/api-tokens/{token_id}", json=kwargs)

    async def revoke_api_token(self, token_id: int) -> dict:
        return await self._request("POST", f"/api/api-tokens/{token_id}/revoke")

    async def delete_api_token(self, token_id: int) -> dict:
        return await self._request("DELETE", f"/api/api-tokens/{token_id}")


class SessionMixin:
    async def forgot_password(self, email: str) -> dict:
        return await self._request("POST", "/api/auth/forgot-password", json={"email": email})

    async def reset_password(self, token: str, new_password: str) -> dict:
        return await self._request("POST", "/api/auth/reset-password", json={"token": token, "new_password": new_password})

    async def verify_email(self, token: str) -> dict:
        return await self._request("POST", "/api/auth/verify-email", json={"token": token})

    async def resend_verification(self) -> dict:
        return await self._request("POST", "/api/auth/resend-verification")

    async def get_sessions(self) -> dict:
        return await self._request("GET", "/api/auth/sessions")

    async def revoke_session(self, session_id: int) -> dict:
        return await self._request("DELETE", f"/api/auth/sessions/{session_id}")

    async def revoke_all_sessions(self) -> dict:
        return await self._request("DELETE", "/api/auth/sessions")


class AuditMixin:
    async def get_audit_logs(self, user_id: int = None, app_id: int = None,
                              action: str = None, resource_type: str = None,
                              start_time: str = None, end_time: str = None,
                              skip: int = 0, limit: int = 100) -> dict:
        params = {"skip": skip, "limit": limit}
        if user_id is not None:
            params["user_id"] = user_id
        if app_id is not None:
            params["app_id"] = app_id
        if action:
            params["action"] = action
        if resource_type:
            params["resource_type"] = resource_type
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        return await self._request("GET", "/api/audit/logs", params=params)

    async def get_audit_actions(self) -> dict:
        return await self._request("GET", "/api/audit/actions")

    async def get_audit_stats(self, app_id: int = None) -> dict:
        params = {}
        if app_id is not None:
            params["app_id"] = app_id
        return await self._request("GET", "/api/audit/stats", params=params)