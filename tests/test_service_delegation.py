"""服务委托（X-On-Behalf-Of）契约测试：fail-closed 三态。

- 合法服务 JWT + 委托头 → 返回委托 user_id；
- 服务 JWT 无委托头 → required 403 / optional None（服务凭证不得当作用户身份）；
- 伪造委托头 + 无效 token → 401（委托头只在验签通过后被消费）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from nexus.auth import AuthDependencies


class StubSDK:
    async def verify_token(self, token: str, permission: str | None = None) -> dict[str, object]:
        if token == "svc":
            return {"success": True, "user_id": "app_31", "role": "service"}
        if token == "user":
            return {"success": True, "user_id": "42", "role": "user"}
        return {"success": False, "detail": "Invalid token"}


class StubRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = headers or {}


def _deps() -> AuthDependencies:
    deps = AuthDependencies(config=object())
    deps.set_sdk(StubSDK())
    return deps


@pytest.mark.asyncio
async def test_service_token_with_delegation_returns_delegated_id() -> None:
    req = StubRequest({"X-On-Behalf-Of": "2"})
    uid = await _deps().get_user_id_required(_cred("svc"), req)
    assert uid == "2"


@pytest.mark.asyncio
async def test_service_token_without_delegation_is_403_on_required() -> None:
    with pytest.raises(HTTPException) as exc:
        await _deps().get_user_id_required(_cred("svc"), StubRequest())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_service_token_without_delegation_is_anonymous_on_optional() -> None:
    uid = await _deps().get_user_id_optional(_cred("svc"), StubRequest())
    assert uid is None


@pytest.mark.asyncio
async def test_user_token_ignores_delegation_header() -> None:
    req = StubRequest({"X-On-Behalf-Of": "999"})
    uid = await _deps().get_user_id_required(_cred("user"), req)
    assert uid == "42"


@pytest.mark.asyncio
async def test_forged_delegation_with_bad_token_is_401() -> None:
    req = StubRequest({"X-On-Behalf-Of": "2"})
    with pytest.raises(HTTPException) as exc:
        await _deps().get_user_id_required(_cred("forged"), req)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_delegation_id_shape_is_validated() -> None:
    req = StubRequest({"X-On-Behalf-Of": "../etc"})
    with pytest.raises(HTTPException) as exc:
        await _deps().get_user_id_required(_cred("svc"), req)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_optional_delegation_returns_delegated_id() -> None:
    req = StubRequest({"X-On-Behalf-Of": "7"})
    uid = await _deps().get_user_id_optional(_cred("svc"), req)
    assert uid == "7"


def _cred(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
