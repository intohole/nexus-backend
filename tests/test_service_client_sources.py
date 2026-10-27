"""ServiceClient 凭证源链测试：uc_auth 活源优先、逐级回退、同凭证去重。"""
from __future__ import annotations

import asyncio

import pytest

from nexus.service_client import ServiceClient


def _fresh_client() -> ServiceClient:
    c = ServiceClient()
    c._client = None
    return c


def test_uc_auth_source_wins_over_stale_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产判例（challengePlanet 10-24）：env app_key 已被 UC 轮换作废，
    uc_auth 活源应先试且成功后不再碰死源。"""
    from nexus import infra

    async def fake_uc_config() -> dict:
        return {"base_url": "http://uc:8901", "app_key": "app_dead", "app_secret": "s_dead"}

    async def fake_uc_auth() -> dict:
        return {"app_key": "app_live", "app_secret": "s_live"}

    monkeypatch.setattr(infra, "get_uc_config", fake_uc_config)
    monkeypatch.setattr(infra, "get_uc_auth", fake_uc_auth)

    exchanges: list[str] = []

    async def fake_exchange(self, key: str, secret: str, base_url: str):
        exchanges.append(key)
        if key == "app_live":
            return "tok", 900
        return "", 0

    monkeypatch.setattr(ServiceClient, "_exchange_token", fake_exchange)
    c = _fresh_client()
    asyncio.run(c._refresh_locked())
    assert c._token == "tok"
    assert exchanges == ["app_live"]


def test_falls_through_to_infra_when_uc_auth_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import infra

    async def fake_uc_config() -> dict:
        return {"base_url": "", "app_key": "app_infra", "app_secret": "s_infra"}

    async def fake_uc_auth() -> dict:
        return {}

    monkeypatch.setattr(infra, "get_uc_config", fake_uc_config)
    monkeypatch.setattr(infra, "get_uc_auth", fake_uc_auth)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    async def fake_exchange(self, key: str, secret: str, base_url: str):
        assert base_url  # infra base_url 缺失时回落 env/default，不为空串
        return ("tok2", 600) if key == "app_infra" else ("", 0)

    monkeypatch.setattr(ServiceClient, "_exchange_token", fake_exchange)
    c = _fresh_client()
    asyncio.run(c._refresh_locked())
    assert c._token == "tok2"


def test_all_sources_fail_falls_back_to_legacy_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import infra

    async def fake_uc_config() -> dict:
        return {"base_url": "http://uc:8901", "app_key": "app_dead", "app_secret": "s1"}

    async def fake_uc_auth() -> dict:
        return {"app_key": "app_dead2", "app_secret": "s2"}

    monkeypatch.setattr(infra, "get_uc_config", fake_uc_config)
    monkeypatch.setattr(infra, "get_uc_auth", fake_uc_auth)
    monkeypatch.setenv("SERVICE_TOKEN", "legacy_tok")

    async def fake_exchange(self, key: str, secret: str, base_url: str):
        return "", 0

    monkeypatch.setattr(ServiceClient, "_exchange_token", fake_exchange)
    c = _fresh_client()
    asyncio.run(c._refresh_locked())
    assert c._token == "legacy_tok"


def test_same_creds_from_two_sources_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import infra

    async def fake_uc_config() -> dict:
        return {"base_url": "http://uc:8901", "app_key": "", "app_secret": ""}

    async def fake_uc_auth() -> dict:
        return {"app_key": "app_x", "app_secret": "s_x"}

    monkeypatch.setattr(infra, "get_uc_config", fake_uc_config)
    monkeypatch.setattr(infra, "get_uc_auth", fake_uc_auth)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    calls: list[str] = []

    async def fake_exchange(self, key: str, secret: str, base_url: str):
        calls.append(key)
        return "tok", 300

    monkeypatch.setattr(ServiceClient, "_exchange_token", fake_exchange)
    c = _fresh_client()
    asyncio.run(c._refresh_locked())
    assert calls == ["app_x"]
