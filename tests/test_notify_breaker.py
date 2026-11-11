from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from nexus.notify import NotifyClient
from nexus.notify_breaker import ConnectBreaker


def _client() -> NotifyClient:
    return NotifyClient(base_url="http://notify.test", service_token="t")


class _ScriptedHttp:
    def __init__(self, script: list[Exception | httpx.Response]) -> None:
        self._script = list(script)
        self.calls = 0

    async def _next(self) -> object:
        self.calls += 1
        if not self._script:
            raise ConnectionError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        return await self._next()

    async def get(self, *args: object, **kwargs: object) -> httpx.Response:
        return await self._next()

    async def close(self) -> None:
        return None


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"id": 7, "suppressed": False}, request=httpx.Request("POST", "http://x"))


def test_client_uses_split_timeouts() -> None:
    client = _client()
    timeout = client._http._timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 2.0
    assert timeout.read == 10.0


def test_single_connect_failure_keeps_closed() -> None:
    client = _client()
    client._http = _ScriptedHttp([httpx.ConnectError("down"), _ok()])
    first = asyncio.run(client.send(user_id="2", title="t"))
    assert first["status"] == "failed"
    assert first["reason"] == "network"
    second = asyncio.run(client.send(user_id="2", title="t"))
    assert second["status"] == "sent"
    assert client._breaker.state(time.monotonic()) == "closed"


def test_two_connect_failures_open_circuit_and_short_circuit() -> None:
    client = _client()
    fake = _ScriptedHttp([httpx.ConnectTimeout("down"), httpx.ConnectTimeout("down")])
    client._http = fake
    for _ in range(2):
        result = asyncio.run(client.send(user_id="2", title="t"))
        assert result["reason"] == "network"
    assert client._breaker.state(time.monotonic()) == "open"
    third = asyncio.run(client.send(user_id="2", title="t"))
    assert third["status"] == "failed"
    assert third["reason"] == "notify_center_breaker_open"
    assert fake.calls == 2


def test_http_status_error_does_not_trip_breaker() -> None:
    client = _client()
    status_exc = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(422, json={"detail": "bad"}),
    )
    client._http = _ScriptedHttp([status_exc, _ok()])
    first = asyncio.run(client.send(user_id="2", title="t"))
    assert first["reason"] == "http_422"
    assert client._breaker.state(time.monotonic()) == "closed"
    second = asyncio.run(client.send(user_id="2", title="t"))
    assert second["status"] == "sent"


def test_read_timeout_counts_as_reachable() -> None:
    client = _client()
    client._http = _ScriptedHttp([httpx.ReadTimeout("slow"), _ok()])
    first = asyncio.run(client.send(user_id="2", title="t"))
    assert first["reason"] == "network"
    second = asyncio.run(client.send(user_id="2", title="t"))
    assert second["status"] == "sent"
    assert client._breaker.state(time.monotonic()) == "closed"


def test_half_open_probe_success_closes() -> None:
    client = _client()
    client._http = _ScriptedHttp(
        [httpx.ConnectError("down"), httpx.ConnectError("down"), _ok()]
    )
    for _ in range(2):
        asyncio.run(client.send(user_id="2", title="t"))
    assert client._breaker.state(time.monotonic()) == "open"
    client._breaker._opened_at = time.monotonic() - 61.0
    assert client._breaker.state(time.monotonic()) == "half_open"
    recovered = asyncio.run(client.send(user_id="2", title="t"))
    assert recovered["status"] == "sent"
    assert client._breaker.state(time.monotonic()) == "closed"


def test_half_open_probe_failure_reopens_with_fresh_cooldown() -> None:
    client = _client()
    client._http = _ScriptedHttp(
        [httpx.ConnectError("down"), httpx.ConnectError("down"), httpx.ConnectError("still down")]
    )
    for _ in range(2):
        asyncio.run(client.send(user_id="2", title="t"))
    client._breaker._opened_at = time.monotonic() - 61.0
    probe = asyncio.run(client.send(user_id="2", title="t"))
    assert probe["reason"] == "network"
    assert client._breaker.state(time.monotonic()) == "open"
    blocked = asyncio.run(client.send(user_id="2", title="t"))
    assert blocked["reason"] == "notify_center_breaker_open"


def test_stats_returns_degraded_when_open() -> None:
    client = _client()
    client._http = _ScriptedHttp([httpx.ConnectError("down"), httpx.ConnectError("down")])
    asyncio.run(client.send(user_id="2", title="t"))
    asyncio.run(client.send(user_id="2", title="t"))
    stats = asyncio.run(client.stats(days=7))
    # r33 失败自显：降级态附 stats_error 键（消费方不再把请求失败误读成「无行」）
    assert stats["apps"] == []
    assert "breaker_open" in stats.get("stats_error", "")


def test_read_status_returns_empty_when_open() -> None:
    client = _client()
    client._http = _ScriptedHttp([httpx.ConnectError("down"), httpx.ConnectError("down")])
    asyncio.run(client.send(user_id="2", title="t"))
    asyncio.run(client.send(user_id="2", title="t"))
    statuses = asyncio.run(client.read_status([1, 2]))
    assert statuses == {}


def test_breaker_unit_state_transitions() -> None:
    breaker = ConnectBreaker(threshold=2, cooldown_s=60.0)
    now = 1000.0
    assert breaker.state(now) == "closed"
    assert breaker.allow(now)
    breaker.on_connect_failure(now)
    assert breaker.state(now + 1) == "closed"
    breaker.on_connect_failure(now + 2)
    assert breaker.state(now + 3) == "open"
    assert not breaker.allow(now + 30)
    assert breaker.allow(now + 62)
    assert breaker.state(now + 62) == "half_open"
    breaker.on_success()
    assert breaker.state(now + 63) == "closed"
    breaker.on_connect_failure(now + 64)
    breaker.on_connect_failure(now + 65)
    assert breaker.state(now + 65) == "open"
    assert breaker.allow(now + 125)
    breaker.on_connect_failure(now + 125)
    assert breaker.state(now + 126) == "open"
    assert not breaker.allow(now + 150)
    assert breaker.allow(now + 185)
    assert breaker.state(now + 185) == "half_open"
    breaker.on_reachable()
    assert breaker.state(now + 185) == "half_open"


def test_breaker_open_error_is_subclass_of_exception() -> None:
    from nexus.notify_breaker import BreakerOpenError

    with pytest.raises(Exception, match="notify_center_breaker_open"):
        raise BreakerOpenError("notify_center_breaker_open")
