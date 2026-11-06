"""UC SDK 能力混入：用户/应用/会员等接口分组（零消费方法族已随 r169 死码批退役）。"""
from typing import Dict, Any


class UserMixin:
    async def get_current_user(self, token: str = None) -> Dict[str, Any]:
        return await self._request("GET", "/api/users/me", token=token)

    async def update_current_user(self, update_data: Dict[str, Any], token: str = None) -> Dict[str, Any]:
        return await self._request("PUT", "/api/users/me", token=token, json=update_data)

    async def get_users(self, token: str = None, skip: int = 0, limit: int = 100) -> Dict[str, Any]:
        return await self._request("GET", f"/api/users?skip={skip}&limit={limit}", token=token)

    async def get_user(self, token: str, user_id: int) -> Dict[str, Any]:
        return await self._request("GET", f"/api/users/{user_id}", token=token)

    async def get_user_contact(self, user_id: int, token: str = None) -> Dict[str, Any]:
        return await self._request("GET", f"/api/users/{user_id}/contact", token=token)

    async def update_user(self, token: str, user_id: int, update_data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PUT", f"/api/users/{user_id}", token=token, json=update_data)

    async def delete_user(self, token: str, user_id: int) -> Dict[str, Any]:
        return await self._request("DELETE", f"/api/users/{user_id}", token=token)


class AppMixin:
    async def get_login_page_config(self, app_key: str = None) -> Dict[str, Any]:
        key = app_key or self.app_key
        return await self._request("GET", f"/api/auth/login-page-config?app_key={key}")


class VipMixin:
    async def get_vip_levels(self) -> dict:
        return await self._request("GET", "/api/vip/levels", params={"app_key": self.app_key})


class SessionMixin:
    async def forgot_password(self, email: str = None, phone: str = None) -> dict:
        data: dict[str, Any] = {}
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        return await self._request("POST", "/api/auth/forgot-password", json=data)

    async def reset_password(self, code: str, new_password: str, email: str = None, phone: str = None) -> dict:
        data: dict[str, Any] = {"code": code, "new_password": new_password}
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        return await self._request("POST", "/api/auth/reset-password", json=data)

    async def change_password(self, old_password: str, new_password: str, revoke_others: bool = True, token: str = None) -> dict:
        return await self._request(
            "POST", "/api/auth/change-password",
            token=token,
            json={"old_password": old_password, "new_password": new_password, "revoke_others": revoke_others},
        )

    async def send_bind_code(self, email: str = None, phone: str = None, token: str = None) -> dict:
        data: dict[str, Any] = {}
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        return await self._request("POST", "/api/auth/send-bind-code", token=token, json=data)

    async def bind_contact(self, code: str, email: str = None, phone: str = None, token: str = None) -> dict:
        data: dict[str, Any] = {"code": code}
        if email:
            data["email"] = email
        if phone:
            data["phone"] = phone
        return await self._request("PUT", "/api/auth/bind-contact", token=token, json=data)

    async def get_sessions(self, token: str = None) -> dict:
        return await self._request("GET", "/api/auth/sessions", token=token)

    async def revoke_session(self, session_id: int, token: str = None) -> dict:
        return await self._request("DELETE", f"/api/auth/sessions/{session_id}", token=token)

    async def revoke_all_sessions(self, token: str = None) -> dict:
        return await self._request("DELETE", "/api/auth/sessions", token=token)


class InviteCodeMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""


class ThirdPartyMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""


class DiscoveryMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""


class ApiTokenMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""


class AuditMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""


class QuotaMixin:
    """组合槽位：方法族已退役，类保留维持 UserCenterSDK MRO。"""
