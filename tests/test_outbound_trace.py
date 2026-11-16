import pytest

import nexus.notify as notify_mod
from nexus.context import set_request_context
from nexus.notify import NotifyClient
from nexus.service_client import ServiceClient


@pytest.mark.asyncio
async def test_notify_headers_carries_request_id(monkeypatch) -> None:
    async def _fake_token() -> str:
        return "svc-token"

    monkeypatch.setattr(notify_mod, "get_service_token", _fake_token)
    set_request_context(request_id="")
    client = NotifyClient(base_url="http://nc", service_token="")
    headers = await client._headers()
    assert headers["X-Service-Token"] == "svc-token"
    assert headers["X-Request-ID"]
    assert len(headers["X-Request-ID"]) == 16


@pytest.mark.asyncio
async def test_notify_headers_request_id_follows_context(monkeypatch) -> None:
    async def _fake_token() -> str:
        return "svc-token"

    monkeypatch.setattr(notify_mod, "get_service_token", _fake_token)
    set_request_context(request_id="req-abc-123")
    client = NotifyClient(base_url="http://nc", service_token="preset")
    headers = await client._headers()
    assert headers["X-Service-Token"] == "preset"
    assert headers["X-Request-ID"] == "req-abc-123"


@pytest.mark.asyncio
async def test_notify_headers_without_token_keeps_trace(monkeypatch) -> None:
    async def _fake_token() -> str:
        return ""

    monkeypatch.setattr(notify_mod, "get_service_token", _fake_token)
    set_request_context(request_id="req-no-token")
    client = NotifyClient(base_url="http://nc", service_token="")
    headers = await client._headers()
    assert "X-Service-Token" not in headers
    assert headers["X-Request-ID"] == "req-no-token"


@pytest.mark.asyncio
async def test_service_client_header_carries_request_id(monkeypatch) -> None:
    async def _fake_token(self) -> str:
        return "uc-token"

    monkeypatch.setattr(ServiceClient, "get_token", _fake_token)
    set_request_context(request_id="req-uc-1")
    client = ServiceClient()
    headers = await client.header()
    assert headers["Authorization"] == "Bearer uc-token"
    assert headers["X-Request-ID"] == "req-uc-1"


@pytest.mark.asyncio
async def test_service_client_delegation_headers_carries_request_id(monkeypatch) -> None:
    async def _fake_token(self) -> str:
        return "uc-token"

    monkeypatch.setattr(ServiceClient, "get_token", _fake_token)
    set_request_context(request_id="req-dele-1")
    client = ServiceClient()
    headers = await client.delegation_headers("u-9")
    assert headers["X-On-Behalf-Of"] == "u-9"
    assert headers["X-Request-ID"] == "req-dele-1"


@pytest.mark.asyncio
async def test_delegation_headers_empty_user_no_trace_leak(monkeypatch) -> None:
    async def _fake_token(self) -> str:
        return "uc-token"

    monkeypatch.setattr(ServiceClient, "get_token", _fake_token)
    set_request_context(request_id="req-empty-user")
    client = ServiceClient()
    headers = await client.delegation_headers("")
    assert headers == {}
