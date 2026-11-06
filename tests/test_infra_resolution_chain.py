from __future__ import annotations

import pytest

from nexus import discovery, infra
from nexus.discovery import ServiceEndpoint, ServiceRegistry


class FakeRegistry(ServiceRegistry):
    def __init__(self, endpoints: dict[str, ServiceEndpoint] | None = None) -> None:
        super().__init__(cache_path="/tmp/nexus_test_no_snapshot.json", ttl_s=10**9)
        self._endpoints = endpoints or {}
        self._node_hosts = {"edge-01": "10.100.0.1"}
        self._state = "cache"

    async def refresh(self, force: bool = False) -> bool:
        return False


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(discovery, "get_registry", lambda: FakeRegistry())
    yield
    discovery.reset_registry()


@pytest.mark.asyncio
async def test_registry_wins_over_env(monkeypatch):
    monkeypatch.setattr(discovery, "get_registry", lambda: FakeRegistry({
        "notifycenter": ServiceEndpoint(name="notifyCenter", port=8910, node_tag="edge-01"),
    }))
    monkeypatch.setenv("NOTIFY_CENTER_URL", "http://env-override:1")
    assert await infra.get_notify_center_url() == "http://10.100.0.1:8910"


@pytest.mark.asyncio
async def test_env_beats_default_when_registry_misses(monkeypatch):
    monkeypatch.setenv("NOTIFY_CENTER_URL", "http://env-override:1")
    assert await infra.get_notify_center_url() == "http://env-override:1"


@pytest.mark.asyncio
async def test_lion_layer_retired_when_registry_and_env_miss(monkeypatch):
    """r171 投影单源化：Lion infra 地址覆盖层退役，注册表/env 双失直达 defaults。"""
    from nexus.defaults import DEFAULT_NOTIFY_CENTER_URL

    async def fake_infra(key: str) -> dict:
        return {"base_url": "http://lion-legacy:2"}

    monkeypatch.delenv("NOTIFY_CENTER_URL", raising=False)
    monkeypatch.setattr(infra, "_get_infra", fake_infra)
    assert await infra.get_notify_center_url() == DEFAULT_NOTIFY_CENTER_URL


@pytest.mark.asyncio
async def test_default_survives_all_misses(monkeypatch):
    from nexus.defaults import DEFAULT_NOTIFY_CENTER_URL

    async def fake_infra(key: str) -> dict:
        return {}

    monkeypatch.delenv("NOTIFY_CENTER_URL", raising=False)
    monkeypatch.setattr(infra, "_get_infra", fake_infra)
    assert await infra.get_notify_center_url() == DEFAULT_NOTIFY_CENTER_URL


@pytest.mark.asyncio
async def test_uc_config_keeps_lion_credentials(monkeypatch):
    """r171 凭证族判留：_get_infra 仍承载 app_key/app_secret（Lion 凭证权威）。"""

    async def fake_infra(key: str) -> dict:
        return {"app_key": "ak-lion", "app_secret": "sec-lion"}

    monkeypatch.setattr(infra, "_get_infra", fake_infra)
    monkeypatch.setattr(discovery, "get_registry", lambda: FakeRegistry({
        "usercenter": ServiceEndpoint(name="usercenter", port=8901, node_tag="edge-01"),
    }))
    config = await infra.get_uc_config()
    assert config["base_url"] == "http://10.100.0.1:8901"
    assert config["app_key"] == "ak-lion"
    assert config["app_secret"] == "sec-lion"
