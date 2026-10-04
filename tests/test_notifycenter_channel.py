"""notifyCenter 直发渠道单元测试."""
from __future__ import annotations

import asyncio

import pytest

from nexus.channels.base import VALID_CHANNELS
from nexus.channels.notifycenter import NotifyCenterChannel


def test_notifycenter_in_valid_channels() -> None:
    assert "notifycenter" in VALID_CHANNELS


def test_channel_name() -> None:
    assert NotifyCenterChannel().name == "notifycenter"


def test_send_without_user_id_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict] = []

    class FakeClient:
        async def send(self, **kwargs: object) -> dict:
            called.append(dict(kwargs))
            return {"ok": True}

    import nexus.notify as notify_mod
    monkeypatch.setattr(notify_mod, "get_notify_client", lambda *a, **k: FakeClient())
    ok = asyncio.run(
        NotifyCenterChannel().send({"title": "t", "content": "c"})
    )
    assert ok is False
    assert called == []


def test_send_passes_fields_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict] = []

    class FakeClient:
        async def send(self, **kwargs: object) -> dict:
            called.append(dict(kwargs))
            return {"id": 1}

    import nexus.notify as notify_mod
    monkeypatch.setattr(notify_mod, "get_notify_client", lambda *a, **k: FakeClient())
    ok = asyncio.run(
        NotifyCenterChannel().send({
            "user_id": "2", "title": "x" * 300, "content": "hello",
            "app_id": "goldenFish", "link": "/goldenfish/workbench",
            "priority": 3, "notify_type": "business",
        })
    )
    assert ok is True
    assert called and called[0]["user_id"] == "2"
    assert called[0]["title"] == "x" * 200
    assert called[0]["app_id"] == "goldenFish"
    assert called[0]["priority"] == 3


def test_send_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        async def send(self, **kwargs: object) -> dict:
            return {}

    import nexus.notify as notify_mod
    monkeypatch.setattr(notify_mod, "get_notify_client", lambda *a, **k: FakeClient())
    ok = asyncio.run(
        NotifyCenterChannel().send({"user_id": "2", "title": "t"})
    )
    assert ok is False


def test_dispatcher_registers_notifycenter() -> None:
    from nexus.channels.dispatcher import ChannelDispatcher

    d = ChannelDispatcher()
    d.init_default_channels()
    assert "notifycenter" in d.get_registered_channels()
