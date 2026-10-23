from __future__ import annotations

import asyncio

import httpx

from nexus.notify import NotifyClient


def _client() -> NotifyClient:
    return NotifyClient(base_url="http://notify.test", service_token="t")


class _FakeHttp:
    def __init__(self, resp: httpx.Response | None = None, exc: Exception | None = None) -> None:
        self._resp = resp
        self._exc = exc
        self.last_params: dict | None = None

    async def get(self, *args: object, **kwargs: object) -> httpx.Response:
        if self._exc is not None:
            raise self._exc
        self.last_params = kwargs.get("params")
        assert isinstance(self._resp, httpx.Response)
        return self._resp

    async def close(self) -> None:
        return None


def test_read_status_maps_exists_rows() -> None:
    client = _client()
    http = _FakeHttp(resp=httpx.Response(200, json={"statuses": [
        {"id": 7, "exists": True, "is_read": False},
        {"id": 8, "exists": True, "is_read": True},
        {"id": 9, "exists": False, "is_read": False},
    ]}, request=httpx.Request("GET", "http://notify.test/api/notify/read-status")))
    client._http = http  # type: ignore[assignment]
    result = asyncio.run(client.read_status([7, 8, 9]))
    assert result == {7: False, 8: True}
    assert http.last_params == {"ids": "7,8,9"}


def test_read_status_error_returns_empty_not_reads() -> None:
    """教训#373：证据链缺失必须返回空 dict（消费方按「缺失」分支处理），
    不能把失败伪装成「全部已读」或「全部未读」。"""
    client = _client()
    client._http = _FakeHttp(exc=httpx.HTTPStatusError("boom", request=httpx.Request("GET", "http://x"), response=httpx.Response(404, json={"detail": "not found"})))  # type: ignore[assignment]
    assert asyncio.run(client.read_status([1, 2])) == {}


def test_read_status_empty_ids_short_circuits() -> None:
    assert asyncio.run(_client().read_status([])) == {}
