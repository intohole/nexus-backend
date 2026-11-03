from __future__ import annotations

import asyncio

import pytest
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


class _FakeNotifyClient:
    def __init__(self, statuses: dict[int, bool] | None = None, exc: Exception | None = None) -> None:
        self._statuses = statuses or {}
        self._exc = exc

    async def read_status(self, ids: list[int]) -> dict[int, bool]:
        if self._exc is not None:
            raise self._exc
        return {i: v for i, v in self._statuses.items() if i in ids}


def test_unread_status_triple_states(monkeypatch: pytest.MonkeyPatch) -> None:
    """簇4 收归守底：True=未读 / False=已读 / 缺席=None（教训#373 语义随原语单源）。"""
    import nexus.notify as notify_mod
    from nexus.notify import unread_status

    monkeypatch.setattr(
        notify_mod, "_notify_client",
        _FakeNotifyClient(statuses={5: False, 6: True}),
    )
    assert asyncio.run(unread_status(5)) is True
    assert asyncio.run(unread_status(6)) is False
    assert asyncio.run(unread_status(999)) is None
    assert asyncio.run(unread_status(0)) is None


def test_unread_status_client_without_method(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.notify as notify_mod
    from nexus.notify import unread_status

    monkeypatch.setattr(notify_mod, "_notify_client", object())
    assert asyncio.run(unread_status(1)) is None


def test_unread_status_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.notify as notify_mod
    from nexus.notify import unread_status

    monkeypatch.setattr(notify_mod, "_notify_client", _FakeNotifyClient(exc=RuntimeError("boom")))
    assert asyncio.run(unread_status(1)) is None
