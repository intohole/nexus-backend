from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nexus.auth import get_current_user_id_required
from nexus.chat import ChatEngine, LocalChatStore, chat_router


@pytest.mark.asyncio
async def test_truncate_from_removes_anchor_and_after(engine):
    conv = await engine.create_conversation("user1", "testapp")
    async for _ in engine.stream_message("user1", "testapp", conv.id, "第一问"):
        pass
    async for _ in engine.stream_message("user1", "testapp", conv.id, "第二问"):
        pass
    result = await engine.list_messages("user1", conv.id, page_size=50)
    assert result["total"] == 4

    msgs = result["items"]
    second_user = [m for m in msgs if m.role == "user"][-1]
    removed = await engine.store.truncate_from(conv.id, second_user.id)
    assert removed == 2

    after = await engine.list_messages("user1", conv.id, page_size=50)
    assert after["total"] == 2
    assert [m.role for m in after["items"]] == ["user", "assistant"]
    assert after["items"][0].content == "第一问"


@pytest.mark.asyncio
async def test_truncate_from_anchor_missing_returns_none(engine):
    conv = await engine.create_conversation("user1", "testapp")
    assert await engine.store.truncate_from(conv.id, "no-such-id") is None


@pytest.mark.asyncio
async def test_regenerate_replaces_last_answer(engine):
    conv = await engine.create_conversation("user1", "testapp")
    async for _ in engine.stream_message("user1", "testapp", conv.id, "第一问"):
        pass
    async for _ in engine.stream_message("user1", "testapp", conv.id, "第二问"):
        pass

    events = []
    async for event in engine.regenerate("user1", "testapp", conv.id):
        events.append(event)
    assert any(e.get("type") == "done" for e in events)

    result = await engine.list_messages("user1", conv.id, page_size=50)
    assert result["total"] == 4
    roles = [m.role for m in result["items"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert result["items"][2].content == "第二问"
    assert result["items"][3].content == "你好，世界"


@pytest.mark.asyncio
async def test_regenerate_no_user_message_yields_error(engine):
    conv = await engine.create_conversation("user1", "testapp")
    events = []
    async for event in engine.regenerate("user1", "testapp", conv.id):
        events.append(event)
    assert events and events[0].get("type") == "error"


@pytest.mark.asyncio
async def test_router_regenerate_endpoint(engine):
    app = FastAPI()
    app.include_router(chat_router(engine, "testapp"))

    def override_user():
        return "user1"

    app.dependency_overrides[get_current_user_id_required] = override_user
    conv = await engine.create_conversation("user1", "testapp")
    async for _ in engine.stream_message("user1", "testapp", conv.id, "路由问句"):
        pass

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/chat/conversations/{conv.id}/messages/regenerate")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
