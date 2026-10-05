from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus.notify import NotifyClient


def _client() -> NotifyClient:
    return NotifyClient(base_url="http://notify.test", service_token="t")


class _FakeHttp:
    def __init__(self, resp: httpx.Response | None = None, exc: Exception | None = None) -> None:
        self._resp = resp
        self._exc = exc

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        if self._exc is not None:
            raise self._exc
        assert isinstance(self._resp, httpx.Response)
        return self._resp

    async def close(self) -> None:
        return None


def _ok_body() -> httpx.Response:
    return httpx.Response(200, json={"id": 7, "suppressed": False, "deduped": False, "channels_sent": ["in_app"]}, request=httpx.Request("POST", "http://notify.test/api/notify/send"))


def test_send_success_adds_status_sent() -> None:
    client = _client()
    client._http = _FakeHttp(resp=_ok_body())
    result = asyncio.run(client.send(user_id="2", title="t"))
    assert result["status"] == "sent"
    assert result["id"] == 7


def test_send_suppressed_body_maps_status() -> None:
    client = _client()
    client._http = _FakeHttp(resp=httpx.Response(200, json={"id": 0, "suppressed": True, "reason": "app_muted"}, request=httpx.Request("POST", "http://notify.test/api/notify/send")))
    result = asyncio.run(client.send(user_id="2", title="t"))
    assert result["status"] == "suppressed"
    assert result["reason"] == "app_muted"


def test_send_http_error_returns_failed() -> None:
    client = _client()
    client._http = _FakeHttp(exc=httpx.HTTPStatusError("boom", request=httpx.Request("POST", "http://x"), response=httpx.Response(422, json={"detail": "bad"})))
    result = asyncio.run(client.send(user_id="2", title="t"))
    assert result["status"] == "failed"
    assert result["reason"] == "http_422"
    assert result["id"] == 0


def test_send_network_error_returns_failed() -> None:
    client = _client()
    client._http = _FakeHttp(exc=ConnectionError("refused"))
    result = asyncio.run(client.send(user_id="2", title="t"))
    assert result["status"] == "failed"
    assert result["reason"] == "network"
