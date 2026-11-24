"""notify 信封诚实性守底（S200·r243 差距 12）。

差距 12：malformed_body 时信封曾谎报 status=sent（lessons#260 同族——
「没判成不得伪装成判过」）。修复=sent→failed 一行。
"""
from __future__ import annotations

import asyncio

import httpx

from nexus.notify import NotifyClient


class _FakeHttp:
    def __init__(self, resp: httpx.Response) -> None:
        self._resp = resp

    async def post(self, *args: object, **kwargs: object) -> httpx.Response:
        return self._resp

    async def close(self) -> None:
        return None


def test_malformed_body_reports_failed_not_sent() -> None:
    client = NotifyClient(base_url="http://notify.test", service_token="t")
    client._http = _FakeHttp(
        httpx.Response(
            200,
            json=[1, 2, 3],
            request=httpx.Request("POST", "http://notify.test/api/notify/send"),
        )
    )

    result = asyncio.run(client.send(user_id="2", title="t"))

    assert result["status"] == "failed"
    assert result["reason"] == "malformed_body"


def test_dict_body_unaffected() -> None:
    client = NotifyClient(base_url="http://notify.test", service_token="t")
    client._http = _FakeHttp(
        httpx.Response(
            200,
            json={"id": 7, "suppressed": False},
            request=httpx.Request("POST", "http://notify.test/api/notify/send"),
        )
    )

    result = asyncio.run(client.send(user_id="2", title="t"))

    assert result["status"] == "sent"
    assert result["id"] == 7
