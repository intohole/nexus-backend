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


@pytest.mark.asyncio
async def test_get_user_full_delegation_rewrites_user_id_drops_role() -> None:
    """get_user_full 与 user_id_required/optional 委托同语义（userFeedback r9）。

    盲区事故形态：委托头被忽略 → userFeedback get_current_admin 判服务身份
    非 admin → 403 → 被上游「非站长预期缺席」静默吞掉 → 站长台行晨报四连缺席。
    守底：委托形态 user_id 回填 + role 键删除（服务 principal 的 role 不冒充
    委托用户，role-based 校验 fail-closed；ADMIN 白名单型按 user_id 正常工作）。
    """
    req = StubRequest({"X-On-Behalf-Of": "2"})
    user = await _deps().get_user_full(_cred("svc"), req)
    assert user.get("user_id") == "2"
    assert "role" not in user


@pytest.mark.asyncio
async def test_get_user_full_without_delegation_keeps_service_principal() -> None:
    """无委托头维持原行为：返回服务 principal 原样（fail-open 于消费方自判）。"""
    user = await _deps().get_user_full(_cred("svc"), StubRequest())
    assert user.get("user_id") == "app_31"
    assert user.get("role") == "service"


@pytest.mark.asyncio
async def test_get_user_full_user_token_ignores_delegation() -> None:
    """用户 token + 委托头：委托头不被消费（与 required 同语义）。"""
    req = StubRequest({"X-On-Behalf-Of": "999"})
    user = await _deps().get_user_full(_cred("user"), req)
    assert user.get("user_id") == "42"
    assert user.get("role") == "user"


@pytest.mark.asyncio
async def test_get_user_full_forged_delegation_bad_header_is_403() -> None:
    """非法委托头形态显式 403（与 required 同语义，可诊断）。"""
    req = StubRequest({"X-On-Behalf-Of": "../etc"})
    with pytest.raises(HTTPException) as exc:
        await _deps().get_user_full(_cred("svc"), req)
    assert exc.value.status_code == 403


def _cred(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
