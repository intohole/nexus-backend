"""ensure_local_user 服务身份守卫：非整数 user_id（app_N 等服务身份）必须 401，
不得让 int() ValueError 漏成 400「invalid literal」（oneNote r15 生产实锤）。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from nexus.user_auth import create_user_auth


async def _find(db, uid):  # pragma: no cover - 守卫必须在触达前拦截
    raise AssertionError("find_by_uc_id must not be called for service identity")


async def _create(db, uid, info):  # pragma: no cover
    raise AssertionError("create_from_uc must not be called for service identity")


def _deps():
    return create_user_auth(get_db=lambda: None, find_by_uc_id=_find, create_from_uc=_create)


@pytest.mark.asyncio
async def test_service_identity_gets_401_not_value_error() -> None:
    with pytest.raises(HTTPException) as exc:
        await _deps().ensure_local_user(db=object(), uc_info={"user_id": "app_31"})
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_user_id_gets_401() -> None:
    with pytest.raises(HTTPException) as exc:
        await _deps().ensure_local_user(db=object(), uc_info={})
    assert exc.value.status_code == 401
