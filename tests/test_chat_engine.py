from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import MetaHandler
from nexus.auth import get_current_user_id_required
from nexus.chat import ChatEngine, HistoryMiddleware, LocalChatStore, chat_router
from nexus.chat.models import ChatConversation
from nexus.errors import ForbiddenError, NotFoundError


@pytest.mark.asyncio
async def test_conversation_crud(engine):
    conv = await engine.create_conversation("user1", "testapp")
    assert conv.id
    assert conv.title == "测试对话"
    assert conv.status == "active"

    conv2 = await engine.create_conversation("user1", "testapp", title="自定义")
    assert conv2.title == "自定义"

    result = await engine.list_conversations("user1", "testapp")
    assert result["total"] == 2

    got = await engine.get_conversation("user1", conv.id)
    assert got.id == conv.id

    updated = await engine.update_conversation("user1", conv.id, title="新标题")
    assert updated.title == "新标题"

    found = await engine.search_conversations("user1", "testapp", "新标题")
    assert len(found) == 1

    await engine.delete_conversation("user1", conv.id)
    result = await engine.list_conversations("user1", "testapp")
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_ownership_and_notfound(engine):
    conv = await engine.create_conversation("user1", "testapp")
    with pytest.raises(ForbiddenError):
        await engine.get_conversation("user2", conv.id)
    with pytest.raises(NotFoundError):
        await engine.get_conversation("user1", "nonexistent")


@pytest.mark.asyncio
async def test_stream_message(engine):
    conv = await engine.create_conversation("user1", "testapp")
    events = []
    async for event in engine.stream_message("user1", "testapp", conv.id, "你好"):
        events.append(event)
    deltas = [e["content"] for e in events if e.get("type") == "delta"]
    assert "".join(deltas) == "你好，世界"
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["content"] == "你好，世界"
    assert done["message_id"]

    messages = await engine.list_messages("user1", conv.id)
    assert messages["total"] == 2
    assert [m.role for m in messages["items"]] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_send_message(engine):
    conv = await engine.create_conversation("user1", "testapp")
    result = await engine.send_message("user1", "testapp", conv.id, "你好")
    assert result["content"] == "你好，世界"
    assert result["message_id"]


@pytest.mark.asyncio
async def test_history_context(engine):
    conv = await engine.create_conversation("user1", "testapp")
    await engine.send_message("user1", "testapp", conv.id, "第一轮")
    await engine.send_message("user1", "testapp", conv.id, "第二轮")
    messages = await engine.list_messages("user1", conv.id)
    assert messages["total"] == 4


@pytest.mark.asyncio
async def test_meta_persistence(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(engine=db_engine, store=store, middlewares=[HistoryMiddleware(store)])
    chat.register("testapp", MetaHandler())
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "你好")
    got = await chat.get_conversation("user1", conv.id)
    assert got.meta == {"phase": "done", "rounds": 1}
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_event_bus(engine):
    seen = []

    async def listener(event: str, payload: dict):
        seen.append((event, payload["conversation_id"]))

    engine.event_bus.subscribe("conversation.created", listener)
    conv = await engine.create_conversation("user1", "testapp")
    assert seen == [("conversation.created", conv.id)]


@pytest.mark.asyncio
async def test_count_messages_since(tmp_path):
    from datetime import datetime, timedelta, timezone

    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'count.db'}")
    store = LocalChatStore(db_engine)
    conv = await store.create_conversation(ChatConversation(user_id="u1", app_name="app", title="t"))
    await store.add_message(conv.id, "user", "a")
    await store.add_message(conv.id, "user", "b")

    since = datetime.now(timezone.utc) - timedelta(seconds=60)
    assert await store.count_messages_since(conv.id, since) == 2
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await store.count_messages_since(conv.id, future) == 0
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_messages_window_tail_and_cursor(tmp_path):
    import asyncio

    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'window.db'}")
    store = LocalChatStore(db_engine)
    conv = await store.create_conversation(
        ChatConversation(user_id="u1", app_name="app", title="window")
    )
    for i in range(7):
        await store.add_message(conv.id, "user" if i % 2 == 0 else "assistant", f"m{i}")
        await asyncio.sleep(0.002)

    latest = await store.messages_window(conv.id, 3)
    assert [m.content for m in latest["items"]] == ["m4", "m5", "m6"]
    assert latest["total"] == 7
    assert latest["has_more"] is True

    oldest_returned = latest["items"][0]
    older = await store.messages_window(conv.id, 3, (oldest_returned.created_at, oldest_returned.id))
    assert [m.content for m in older["items"]] == ["m1", "m2", "m3"]
    assert older["has_more"] is True

    oldest_returned = older["items"][0]
    rest = await store.messages_window(conv.id, 3, (oldest_returned.created_at, oldest_returned.id))
    assert [m.content for m in rest["items"]] == ["m0"]
    assert rest["has_more"] is False
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_router_messages_window(engine):
    from datetime import datetime

    app = FastAPI()
    app.include_router(chat_router(engine, app_name="testapp"))
    app.dependency_overrides[get_current_user_id_required] = lambda: "user1"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/conversations", json={})
        conv_id = resp.json()["data"]["id"]
        for text in ["第一轮", "第二轮", "第三轮"]:
            await client.post(
                f"/api/chat/conversations/{conv_id}/messages", json={"content": text}
            )

        resp = await client.get(
            f"/api/chat/conversations/{conv_id}/messages", params={"tail": "true", "page_size": 2}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert [m["content"] for m in data["data"]] == ["第三轮", "你好，世界"]
        assert data["pagination"]["has_more"] is True
        cursor = data["pagination"]["next_cursor"]
        cursor_created_at, cursor_id = cursor.rsplit("|", 1)
        datetime.fromisoformat(cursor_created_at)

        resp = await client.get(
            f"/api/chat/conversations/{conv_id}/messages",
            params={"before": cursor, "page_size": 2},
        )
        assert resp.status_code == 200
        assert [m["content"] for m in resp.json()["data"]] == ["第二轮", "你好，世界"]
        assert resp.json()["pagination"]["has_more"] is True

        resp = await client.get(
            f"/api/chat/conversations/{conv_id}/messages", params={"before": "bad-cursor"}
        )
        assert resp.status_code == 422

        resp = await client.get(f"/api/chat/conversations/{conv_id}/messages")
        assert [m["content"] for m in resp.json()["data"]] == [
            "第一轮", "你好，世界", "第二轮", "你好，世界", "第三轮", "你好，世界",
        ]


@pytest.mark.asyncio
async def test_router(engine):
    app = FastAPI()
    app.include_router(chat_router(engine, app_name="testapp"))
    app.dependency_overrides[get_current_user_id_required] = lambda: "user1"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/conversations", json={})
        assert resp.status_code == 200
        conv_id = resp.json()["data"]["id"]
        assert resp.json()["data"]["title"] == "测试对话"

        resp = await client.post(
            f"/api/chat/conversations/{conv_id}/messages", json={"content": "你好"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["content"] == "你好，世界"

        resp = await client.get(f"/api/chat/conversations/{conv_id}/messages")
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 2

        resp = await client.get("/api/chat/conversations")
        assert resp.status_code == 200
        assert resp.json()["pagination"]["total"] == 1

        resp = await client.patch(
            f"/api/chat/conversations/{conv_id}", json={"title": "改标题"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["title"] == "改标题"

        resp = await client.get("/api/chat/conversations/search", params={"q": "改标题"})
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

        resp = await client.delete(f"/api/chat/conversations/{conv_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True


@pytest.mark.asyncio
async def test_router_stream(engine):
    app = FastAPI()
    app.include_router(chat_router(engine, app_name="testapp"))
    app.dependency_overrides[get_current_user_id_required] = lambda: "user1"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/conversations", json={})
        conv_id = resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/chat/conversations/{conv_id}/messages/stream", json={"content": "你好"}
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        events = []
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        deltas = "".join(e.get("content", "") for e in events if e.get("type") == "delta")
        assert deltas == "你好，世界"
        assert any(e.get("type") == "done" for e in events)


@pytest.mark.asyncio
async def test_router_validation(engine):
    app = FastAPI()
    app.include_router(chat_router(engine, app_name="testapp"))
    app.dependency_overrides[get_current_user_id_required] = lambda: "user1"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat/conversations", json={})
        conv_id = resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/chat/conversations/{conv_id}/messages", json={"content": ""}
        )
        assert resp.status_code == 422