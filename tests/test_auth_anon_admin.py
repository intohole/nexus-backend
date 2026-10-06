"""auth_anon 与 require_admin 组合器契约测试（49 轮收归原语）。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import nexus.auth_anon as auth_anon
import nexus.permissions as perms
from nexus.auth_anon import (
    ANON_ID_RE,
    get_current_user_or_anon,
    get_current_user_or_anon_lax,
    get_current_user_or_anon_optional,
)
from nexus.permissions import require_admin, require_admin_id


class FakeDeps:
    def __init__(self, result):
        self._result = result

    async def validate_token(self, token: str):
        if token == "good":
            return {"user_id": "42", "role": self._result}
        return None


def _patch(monkeypatch, role="user"):
    fake = FakeDeps(role)
    monkeypatch.setattr(auth_anon, "get_auth_deps", lambda: fake)
    monkeypatch.setattr(perms, "get_auth_deps", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_valid_token_returns_tuple(monkeypatch):
    _patch(monkeypatch)
    assert await get_current_user_or_anon("Bearer good", None) == ("42", "good")


@pytest.mark.asyncio
async def test_invalid_token_401(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(HTTPException) as e:
        await get_current_user_or_anon("Bearer bad", None)
    assert e.value.status_code == 401
    assert e.value.detail == "未授权或登录已过期"


@pytest.mark.asyncio
async def test_anon_fallback_strict(monkeypatch):
    _patch(monkeypatch)
    assert await get_current_user_or_anon(None, "anon-abcd1234") == ("anon-abcd1234", None)
    with pytest.raises(HTTPException) as e:
        await get_current_user_or_anon(None, None)
    assert e.value.detail == "请先登录"
    with pytest.raises(HTTPException) as e:
        await get_current_user_or_anon(None, "短")
    assert e.value.detail == "匿名标识不合法"


@pytest.mark.asyncio
async def test_guest_fallback_lax(monkeypatch):
    _patch(monkeypatch)
    assert await get_current_user_or_anon_lax(None, None) == ("guest", None)
    assert await get_current_user_or_anon_lax(None, "任意!格式") == ("任意!格式", None)


@pytest.mark.asyncio
async def test_optional_never_raises(monkeypatch):
    _patch(monkeypatch)
    assert await get_current_user_or_anon_optional(None, None) == ("guest", None)
    assert await get_current_user_or_anon_optional("Bearer bad", None) == ("guest", None)
    assert await get_current_user_or_anon_optional("Bearer good", None) == ("42", "good")


@pytest.mark.asyncio
async def test_anon_id_re_shape():
    assert ANON_ID_RE.fullmatch("Ab_-09")
    assert ANON_ID_RE.fullmatch("x" * 64)
    assert not ANON_ID_RE.fullmatch("abc")
    assert not ANON_ID_RE.fullmatch("有中文")


@pytest.mark.asyncio
async def test_require_admin_role_gate(monkeypatch):
    _patch(monkeypatch, role="admin")
    dep = require_admin()
    user = await dep("Bearer good")
    assert user["user_id"] == "42"
    dep_id = require_admin_id()
    assert await dep_id("Bearer good") == "42"

    _patch(monkeypatch, role="user")
    with pytest.raises(HTTPException) as e:
        await dep("Bearer good")
    assert e.value.status_code == 403
    assert e.value.detail == "需要管理员权限"
    with pytest.raises(HTTPException) as e:
        await dep("Bearer bad")
    assert e.value.status_code == 401

    custom = require_admin(detail="仅管理员可执行此操作")
    with pytest.raises(HTTPException) as e:
        await custom("Bearer good")
    assert e.value.detail == "仅管理员可执行此操作"


@pytest.mark.asyncio
async def test_require_admin_missing_token(monkeypatch):
    _patch(monkeypatch, role="admin")
    with pytest.raises(HTTPException) as e:
        await require_admin()(None)
    assert e.value.status_code == 401
