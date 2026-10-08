"""LionIntegration 共享连接池契约测试。"""
import pytest

from nexus.lion import LionIntegration


@pytest.mark.asyncio
async def test_shared_sdk_instance_reused():
    integ = LionIntegration()
    sdk1 = integ._get_lion_sdk()
    sdk2 = integ._get_lion_sdk()
    assert sdk1 is sdk2


@pytest.mark.asyncio
async def test_aclose_resets_and_rebuilds():
    integ = LionIntegration()
    sdk1 = integ._get_lion_sdk()
    await integ.aclose()
    assert integ._lion_sdk is None
    sdk2 = integ._get_lion_sdk()
    assert sdk2 is not sdk1
    await integ.aclose()


@pytest.mark.asyncio
async def test_aclose_on_never_used_instance_is_noop():
    integ = LionIntegration()
    await integ.aclose()
    assert integ._lion_sdk is None


@pytest.mark.asyncio
async def test_business_config_serves_last_good_on_fetch_failure(monkeypatch):
    integ = LionIntegration()

    class _Sdk:
        def __init__(self):
            self.results: list[dict] = []

        async def get_business_config(self, key: str) -> dict:
            return self.results.pop(0)

    sdk = _Sdk()
    monkeypatch.setattr(integ, "_get_lion_sdk", lambda: sdk)

    sdk.results = [{"app_key": "k1", "app_secret": "s1"}]
    cfg = await integ.get_business_config("uc_auth", use_cache=False)
    assert cfg["app_key"] == "k1"

    sdk.results = [{"success": False, "detail": "Cannot connect"}]
    cfg = await integ.get_business_config("uc_auth", use_cache=False)
    assert cfg["app_key"] == "k1"
    assert cfg.get("success") is not False


@pytest.mark.asyncio
async def test_business_config_returns_error_when_never_succeeded(monkeypatch):
    integ = LionIntegration()

    class _Sdk:
        async def get_business_config(self, key: str) -> dict:
            return {"success": False, "detail": "Cannot connect"}

    monkeypatch.setattr(integ, "_get_lion_sdk", lambda: _Sdk())
    cfg = await integ.get_business_config("uc_auth", use_cache=False)
    assert cfg.get("success") is False


@pytest.mark.asyncio
async def test_infra_config_serves_last_good_on_fetch_failure(monkeypatch):
    integ = LionIntegration()

    class _Sdk:
        def __init__(self):
            self.results: list[dict] = []

        async def get_infra_config(self, key: str) -> dict:
            return self.results.pop(0)

    sdk = _Sdk()
    monkeypatch.setattr(integ, "_get_lion_sdk", lambda: sdk)

    sdk.results = [{"base_url": "http://uc:8901"}]
    cfg = await integ.get_infra_config("usercenter", use_cache=False)
    assert cfg["base_url"] == "http://uc:8901"

    sdk.results = [{"success": False, "detail": "timeout"}]
    cfg = await integ.get_infra_config("usercenter", use_cache=False)
    assert cfg["base_url"] == "http://uc:8901"
