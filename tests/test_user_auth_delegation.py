"""user_auth 服务委托（oneNote r18）：服务身份 + X-On-Behalf-Of → 本地影子用户。

对齐 nexus.auth get_user_id_required 的委托语义；无委托头保持 r15 判例 fail-closed 401。
回归背景：11-01/11-02 晨报拾光段两连缺席——nexus-agent 委托通道在 auth.py 形态应用
（gezhi）200、本模块形态应用（oneNote）结构性 401，两套用户鉴权行为漂移。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request as _Request

import nexus.user_auth as ua
from nexus.user_auth import create_user_auth


def _request(headers: dict[str, str] | None = None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": b"",
    }
    return _Request(scope)


def _creds(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _User:
    def __init__(self, uid: int) -> None:
        self.id = uid


def _make_deps(seen: list[int]):
    async def _find(db, uc_id):
        seen.append(uc_id)
        return _User(uc_id)

    async def _create(db, uc_id, info):  # pragma: no cover - 用例不触达创建路径
        raise AssertionError("create_from_uc must not run in delegation tests")

    deps = create_user_auth(get_db=lambda: None, find_by_uc_id=_find, create_from_uc=_create)

    async def _fake_validate(token: str) -> dict:
        # token 形态自描述：svc=服务身份 / usr=真实用户
        if token.startswith("svc"):
            return {"user_id": "app_31", "role": "service"}
        return {"user_id": "2", "username": "lxz"}

    return deps, _fake_validate


def _patch_validate(monkeypatch, fake):
    class _Deps:
        async def validate_token(self, token: str) -> dict:
            return await fake(token)

    monkeypatch.setattr(ua, "get_auth_deps", lambda: _Deps())


@pytest.mark.asyncio
async def test_service_identity_with_delegation_resolves_local_user(monkeypatch) -> None:
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    user = await deps.get_current_user_id(
        credentials=_creds("svc-token"), db=object(), request=_request({"X-On-Behalf-Of": "2"})
    )
    assert user == 2
    assert seen == [2]


@pytest.mark.asyncio
async def test_service_identity_without_delegation_still_401(monkeypatch) -> None:
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_id(
            credentials=_creds("svc-token"), db=object(), request=_request({})
        )
    assert exc.value.status_code == 401
    assert seen == []


@pytest.mark.asyncio
async def test_delegation_never_poisons_token_cache(monkeypatch) -> None:
    """同一服务 token 先代表 2 再代表 3：缓存里的服务身份 payload 不得串用户。"""
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    first = await deps.get_current_user_id(
        credentials=_creds("svc-token"), db=object(), request=_request({"X-On-Behalf-Of": "2"})
    )
    second = await deps.get_current_user_id(
        credentials=_creds("svc-token"), db=object(), request=_request({"X-On-Behalf-Of": "3"})
    )
    assert (first, second) == (2, 3)
    assert seen == [2, 3]


@pytest.mark.asyncio
async def test_invalid_delegation_header_403(monkeypatch) -> None:
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_id(
            credentials=_creds("svc-token"),
            db=object(),
            request=_request({"X-On-Behalf-Of": "bad id!"}),
        )
    assert exc.value.status_code == 403
    assert seen == []


@pytest.mark.asyncio
async def test_user_token_cannot_usurp_via_delegation_header(monkeypatch) -> None:
    """真实用户 token + 委托头：委托只对服务身份生效，用户不得借头换身份。"""
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    uid = await deps.get_current_user_id(
        credentials=_creds("usr-token"), db=object(), request=_request({"X-On-Behalf-Of": "999"})
    )
    assert uid == 2
    assert seen == [2]


@pytest.mark.asyncio
async def test_delegating_to_service_principal_falls_to_guard(monkeypatch) -> None:
    """X-On-Behalf-Of: app_31（正则放行）→ int() 守卫 401，不得建出 user_id=app_31 的影子用户。"""
    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_id(
            credentials=_creds("svc-token"),
            db=object(),
            request=_request({"X-On-Behalf-Of": "app_31"}),
        )
    assert exc.value.status_code == 401
    assert seen == []


def test_dependency_signature_buildable_by_fastapi(monkeypatch) -> None:
    """启动级守底（oneNote r18 本地真链掀雷）：request 形参必须是裸 Request 注解——
    Optional[Request] 会被 FastAPI 当 Pydantic 字段建模，路由注册即
    FastAPIError（pytest 直调依赖函数测不出，必须过一次 include_router）。"""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    seen: list[int] = []
    deps, fake = _make_deps(seen)
    _patch_validate(monkeypatch, fake)
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(uid: int = Depends(deps.get_current_user_id)):
        return {"uid": uid}

    client = TestClient(app)  # 构造即注册路由；签名不合法此处抛 FastAPIError
    r = client.get(
        "/whoami",
        headers={"Authorization": "Bearer svc-token", "X-On-Behalf-Of": "2"},
    )
    assert r.status_code == 200
    assert r.json() == {"uid": 2}
    assert seen == [2]
