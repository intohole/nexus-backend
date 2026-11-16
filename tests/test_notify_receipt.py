"""r221 守底：send_receipt 回执句柄原语三态+占位单源拦截+渠道轨消费回路。"""
from __future__ import annotations

import asyncio

from nexus.channels.notifycenter import NotifyCenterChannel
from nexus.notify_api import send_receipt


class _FakeClient:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def send(self, **kwargs):
        self.calls.append(dict(kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _install(monkeypatch, client: _FakeClient) -> None:
    import nexus.notify as nexus_notify

    monkeypatch.setattr(nexus_notify, "get_notify_client", lambda base_url="": client)


def test_receipt_sent_returns_id_and_envelope(monkeypatch):
    client = _FakeClient([{"status": "sent", "id": 42}])
    _install(monkeypatch, client)
    notify_id, envelope = asyncio.run(send_receipt(
        user_id="2", title="t", type="business", app_id="x",
    ))
    assert notify_id == 42
    assert envelope["status"] == "sent"
    assert client.calls[0]["user_id"] == "2"


def test_receipt_placeholder_refused_without_network(monkeypatch):
    client = _FakeClient([])
    _install(monkeypatch, client)
    for uid in ("", "default", "unknown", "0", "guest", "anon_9f2c", "ANON_x1"):
        notify_id, envelope = asyncio.run(send_receipt(
            user_id=uid, title="t", app_id="x",
        ))
        assert notify_id == 0
        assert envelope["status"] == "suppressed"
        assert envelope["reason"] == "placeholder_user"
    assert client.calls == []


def test_receipt_swallows_client_exception(monkeypatch):
    client = _FakeClient([RuntimeError("down")])
    _install(monkeypatch, client)
    notify_id, envelope = asyncio.run(send_receipt(user_id="2", title="t"))
    assert notify_id == 0
    assert envelope["status"] == "failed"
    assert str(envelope["reason"]).startswith("receipt_error:")


def test_receipt_malformed_body_maps_failed(monkeypatch):
    client = _FakeClient(["not-a-dict"])
    _install(monkeypatch, client)
    notify_id, envelope = asyncio.run(send_receipt(user_id="2", title="t"))
    assert notify_id == 0
    assert envelope["reason"] == "malformed_body"


def test_receipt_suppressed_maps_zero(monkeypatch):
    client = _FakeClient([{"status": "suppressed", "id": 0, "reason": "app_muted"}])
    _install(monkeypatch, client)
    notify_id, envelope = asyncio.run(send_receipt(user_id="2", title="t"))
    assert notify_id == 0
    assert envelope["reason"] == "app_muted"


def test_receipt_truncates_title_content_and_strips_user(monkeypatch):
    client = _FakeClient([{"status": "sent", "id": 1}])
    _install(monkeypatch, client)
    asyncio.run(send_receipt(
        user_id=" 2 ", title="长" * 300, content="字" * 3000, app_id="x",
    ))
    call = client.calls[0]
    assert call["user_id"] == "2"
    assert len(call["title"]) == 200
    assert len(call["content"]) == 2000


def test_receipt_sent_without_id_is_zero(monkeypatch):
    client = _FakeClient([{"status": "sent", "id": 0, "reason": "malformed_body"}])
    _install(monkeypatch, client)
    notify_id, envelope = asyncio.run(send_receipt(user_id="2", title="t"))
    assert notify_id == 0
    assert envelope["status"] == "sent"


def test_channel_send_consumes_receipt_true_false(monkeypatch):
    client = _FakeClient([
        {"status": "sent", "id": 7},
        {"status": "failed", "id": 0, "reason": "http_500"},
    ])
    _install(monkeypatch, client)
    channel = NotifyCenterChannel()
    assert asyncio.run(channel.send({"user_id": "2", "title": "t"})) is True
    assert asyncio.run(channel.send({"user_id": "2", "title": "t"})) is False
    assert client.calls[0]["app_id"] == "system"
    assert client.calls[0]["channels"] is None


def test_channel_placeholder_refused_no_http(monkeypatch):
    client = _FakeClient([])
    _install(monkeypatch, client)
    channel = NotifyCenterChannel()
    assert asyncio.run(channel.send({"user_id": "default", "title": "t"})) is False
    assert asyncio.run(channel.send({"user_id": "anon_abc", "title": "t"})) is False
    assert client.calls == []


def test_channel_normalizes_and_maps_notification_dict(monkeypatch):
    client = _FakeClient([{"status": "sent", "id": 9}])
    _install(monkeypatch, client)
    channel = NotifyCenterChannel()
    ok = asyncio.run(channel.send({
        "user_id": "3", "title": "t", "content": "c",
        "notify_type": "business", "priority": 2, "app_id": "gf",
        "link": "/goldenfish/workbench",
        "channels": ["email", "", "wecom"],
        "dedup_key": "k1",
    }))
    assert ok is True
    call = client.calls[0]
    assert call["type"] == "business"
    assert call["channels"] == ["email", "wecom"]
    assert call["dedup_key"] == "k1"
    assert call["link"] == "/goldenfish/workbench"


def test_receipt_exported_from_notify_package():
    import nexus.notify as notify_mod

    assert hasattr(notify_mod, "send_receipt")
