"""UC SDK 认证流程层：登录/注册/刷新/注销，组合 TokenVerifyMixin 为 AuthMixin。"""
import time
from typing import Dict, Any

from .auth_verify import TokenVerifyMixin


class AuthFlowMixin:

    async def login(self, username: str = None, email: str = None, phone: str = None,
                    password: str = None, invite_code: str = None) -> Dict[str, Any]:
        guard = self._credential_guard()
        if guard is not None:
            return guard
        data = {"password": password, "app_key": self.app_key}
        if username:
            data["username"] = username
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        if invite_code:
            data["invite_code"] = invite_code
        result = await self._request("POST", "/api/auth/login", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def register(self, username: str = None, email: str = None, phone: str = None,
                       password: str = None, invite_code: str = None) -> Dict[str, Any]:
        guard = self._credential_guard()
        if guard is not None:
            return guard
        data = {"password": password, "app_key": self.app_key}
        if username:
            data["username"] = username
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        if invite_code:
            data["invite_code"] = invite_code
        result = await self._request("POST", "/api/auth/register", json=data)
        if result.get("success") and result.get("data"):
            self._set_tokens(result["data"])
        return result

    async def refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return False
        try:
            client = await self._get_client()
            response = await client.post(
                "/api/auth/refresh",
                json={"refresh_token": self._refresh_token}
            )
            if response.status_code >= 400:
                self.clear_tokens()
                return False
            result = response.json()
            if result.get("success") and result.get("data"):
                self._set_tokens(result["data"])
                return True
        except Exception:
            self.clear_tokens()
        return False

    async def refresh_with_token(self, refresh_token: str) -> Dict[str, Any] | None:
        try:
            client = await self._get_client()
            response = await client.post(
                "/api/auth/refresh",
                json={"refresh_token": refresh_token}
            )
            if response.status_code >= 400:
                return None
            result = response.json()
            if result.get("success") and result.get("data"):
                data = result["data"]
                return {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token", refresh_token),
                    "token_type": data.get("token_type", "bearer"),
                    "expires_in": data.get("expires_in")
                }
        except Exception:
            pass
        return None

    async def logout(self, token: str = None) -> None:
        try:
            await self._request("POST", "/api/auth/logout", token=token, skip_refresh=True)
        except Exception:
            pass
        self.clear_tokens()

    async def client_credentials(self) -> Dict[str, Any]:
        if not self.app_secret:
            raise ValueError("app_secret is required for client_credentials")
        client = await self._get_client()
        response = await client.post(
            "/api/auth/token",
            json={
                "grant_type": "client_credentials",
                "app_key": self.app_key,
                "app_secret": self.app_secret
            }
        )
        response.raise_for_status()
        result = response.json()
        if result.get("success") and result.get("data"):
            self._access_token = result["data"].get("access_token")
            if result["data"].get("expires_in"):
                self._token_expires_at = time.time() + result["data"]["expires_in"]
        return result

    async def check_permission(self, token: str, permission: str) -> Dict[str, Any]:
        return await self._request(
            "POST", "/api/auth/check-permission",
            json={"token": token, "permission": permission}
        )


class AuthMixin(AuthFlowMixin, TokenVerifyMixin):
    """组合门面：保持原 AuthMixin 名称与 MRO，client.py 导入零改动。"""
