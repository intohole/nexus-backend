"""AuthDependencies.validate_token 契约测试：验证成功必须返回结果，不得被悬空引用吞成 401。"""
from __future__ import annotations

import pytest

from nexus.auth import AuthDependencies


class StubSDK:
    async def verify_token(self, token: str, permission: str | None = None) -> dict[str, object]:
        if token == "good":
            return {"success": True, "user_id": "42", "role": "user"}
        return {"success": False, "detail": "Invalid token"}


def _deps() -> AuthDependencies:
    deps = AuthDependencies(config=object())
    deps.set_sdk(StubSDK())
    return deps


@pytest.mark.asyncio
async def test_validate_token_success_is_not_swallowed() -> None:
    result = await _deps().validate_token("good")
    assert result is not None
    assert result["user_id"] == "42"


@pytest.mark.asyncio
async def test_validate_token_invalid_returns_none() -> None:
    assert await _deps().validate_token("bad") is None


@pytest.mark.asyncio
async def test_validate_token_blank_returns_none() -> None:
    assert await _deps().validate_token("   ") is None
