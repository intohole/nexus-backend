from __future__ import annotations

import asyncio

import pytest

import nexus.infra as infra
import nexus.ironman as ironman_mod
import nexus.uc_sdk_helper as helper


@pytest.fixture(autouse=True)
def _reset_helper_state(monkeypatch):
    async def _noop_bootstrap(sdk) -> None:
        return None

    monkeypatch.setattr(helper, "_bootstrap", _noop_bootstrap)
    yield
    task = helper._credential_recovery_task
    if task is not None and not task.done():
        task.cancel()
    helper._credential_recovery_task = None
    helper._sdk = None


@pytest.mark.asyncio
async def test_uc_credentials_recovered_after_lion_outage(monkeypatch):
    monkeypatch.setattr(helper, "CREDENTIAL_RECOVERY_INITIAL_DELAY", 0.01)
    monkeypatch.setattr(helper, "CREDENTIAL_RECOVERY_MAX_DELAY", 0.02)
    calls: dict[str, int] = {"n": 0}

    async def fake_get_uc_auth() -> dict[str, str]:
        calls["n"] += 1
        if calls["n"] <= 2:
            return {"app_key": "", "app_secret": ""}
        return {"app_key": "app_recovered", "app_secret": "secret_recovered"}

    async def fake_get_uc_base_url() -> str:
        return "http://127.0.0.1:8901"

    monkeypatch.setattr(infra, "get_uc_auth", fake_get_uc_auth)
    monkeypatch.setattr(infra, "get_uc_base_url", fake_get_uc_base_url)

    sdk = await helper.init_uc_sdk_from_lion()
    assert not sdk.app_key
    assert not sdk.app_secret
    task = helper._credential_recovery_task
    assert task is not None
    await asyncio.wait_for(task, timeout=3)
    assert sdk.app_key == "app_recovered"
    assert sdk.app_secret == "secret_recovered"
    assert sdk.client_id == "app_recovered"
    assert sdk._app_secret == "secret_recovered"


@pytest.mark.asyncio
async def test_no_recovery_task_when_credentials_present(monkeypatch):
    async def fake_get_uc_auth() -> dict[str, str]:
        return {"app_key": "app_ok", "app_secret": "secret_ok"}

    async def fake_get_uc_base_url() -> str:
        return "http://127.0.0.1:8901"

    monkeypatch.setattr(infra, "get_uc_auth", fake_get_uc_auth)
    monkeypatch.setattr(infra, "get_uc_base_url", fake_get_uc_base_url)

    sdk = await helper.init_uc_sdk_from_lion()
    assert sdk.app_key == "app_ok"
    assert helper._credential_recovery_task is None


@pytest.mark.asyncio
async def test_login_fails_fast_without_credentials():
    from nexus.uc_sdk import UserCenterSDK

    sdk = UserCenterSDK(base_url="http://127.0.0.1:1", app_key="", app_secret="")
    result = await sdk.login(username="lxz", password="12345678")
    assert result["success"] is False
    assert "认证服务未就绪" in str(result["detail"])
    assert result["status_code"] == 503
    register_result = await sdk.register(username="lxz", password="12345678")
    assert register_result["success"] is False
    assert register_result["status_code"] == 503


def test_login_route_passes_through_status_code():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from nexus.auth_routes import create_auth_router

    class _StubSDK:
        async def login(self, **kwargs):
            return {
                "success": False,
                "detail": "认证服务未就绪（应用凭证未同步），请稍后重试",
                "status_code": 503,
            }

    app = FastAPI()
    app.include_router(
        create_auth_router(prefix="/uc-auth", uc_sdk_provider=lambda: _StubSDK(), endpoints={"login"})
    )
    response = TestClient(app).post("/uc-auth/login", json={"username": "lxz", "password": "x"})
    assert response.status_code == 503
    assert "认证服务未就绪" in response.text


@pytest.mark.asyncio
async def test_incomplete_ironman_config_not_cached(monkeypatch):
    import ironman as ironman_pkg

    class _IncompleteBootstrap:
        def is_available(self) -> bool:
            return False

    class _FakeBootstrap:
        @staticmethod
        async def create(**kwargs):
            return _IncompleteBootstrap()

    monkeypatch.setattr(ironman_pkg, "Bootstrap", _FakeBootstrap, raising=False)
    await ironman_mod.reload_ironman()
    with pytest.raises(ironman_mod.IronmanConfigError):
        await ironman_mod.init_ironman(app_name="testApp")
    assert ironman_mod.get_bootstrap() is None