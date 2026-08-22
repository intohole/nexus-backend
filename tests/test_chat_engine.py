from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.auth import get_current_user_id_required
from nexus.chat import (
    BaseChatHandler,
    ChatEngine,
    CostMiddleware,
    HistoryMiddleware,
    LocalChatStore,
    RateLimitMiddleware,
    TitleMiddleware,
    chat_router,
)
from nexus.chat.context import ChatContext
from nexus.errors import ForbiddenError, NotFoundError


class FakeHandler(BaseChatHandler):
    default_title = "测试对话"

    async def stream_reply(self, context: ChatContext, messages: list[dict[str, str]]):
        for chunk in ["你好", "，", "世界"]:
            yield chunk


class MetaHandler(FakeHandler):
    async def on_reply_complete(
        self, context: ChatContext, user_message: str, reply: str
    ) -> dict | None:
        return {"phase": "done", "rounds": 1}


@pytest_asyncio.fixture
async def engine(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store)],
    )
    chat.register("testapp", FakeHandler())
    yield chat
    await db_engine.dispose()


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
async def test_rate_limit_middleware(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rate.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), RateLimitMiddleware(store, max_messages=1, window_seconds=3600)],
    )
    chat.register("testapp", FakeHandler())
    conv = await chat.create_conversation("user1", "testapp")

    ok = []
    async for event in chat.stream_message("user1", "testapp", conv.id, "第一条"):
        ok.append(event)
    assert any(e.get("type") == "done" for e in ok)

    blocked = []
    async for event in chat.stream_message("user1", "testapp", conv.id, "第二条超限"):
        blocked.append(event)
    first = blocked[0]
    assert first.get("type") == "error"
    assert first.get("code") == 429
    assert len(blocked) == 1
    assert not any(e.get("type") == "done" for e in blocked)
    assert not any(e.get("type") == "delta" for e in blocked)
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_count_messages_since(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'count.db'}")
    store = LocalChatStore(db_engine)
    conv = await store.create_conversation(
        __import__("nexus.chat.models", fromlist=["ChatConversation"]).ChatConversation(
            user_id="u1", app_name="app", title="t"
        )
    )
    await store.add_message(conv.id, "user", "a")
    await store.add_message(conv.id, "user", "b")
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(seconds=60)
    assert await store.count_messages_since(conv.id, since) == 2
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    assert await store.count_messages_since(conv.id, future) == 0
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_middleware(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cost.db'}")
    store = LocalChatStore(db_engine)
    cost_mw = CostMiddleware(store, input_price_per_1k=1.0, output_price_per_1k=2.0)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), cost_mw],
    )
    chat.register("testapp", FakeHandler())
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "你好")
    got = await chat.get_conversation("user1", conv.id)
    stats = got.meta["cost"]
    assert stats["rounds"] == 1
    assert stats["tokens"] > 0
    assert stats["amount"] >= 0
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_usage_report(tmp_path):
    class UsageHandler(FakeHandler):
        async def stream_reply(self, context: ChatContext, messages):
            context.set_usage(prompt_tokens=100, completion_tokens=50)
            yield "回复"

    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), CostMiddleware(store)],
    )
    chat.register("testapp", UsageHandler())
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "hi")
    got = await chat.get_conversation("user1", conv.id)
    assert got.meta["cost"]["tokens"] == 150
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_budget(tmp_path):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), CostMiddleware(store, token_budget=1)],
    )
    chat.register("testapp", FakeHandler())
    conv = await chat.create_conversation("user1", "testapp")

    async for _ in chat.stream_message("user1", "testapp", conv.id, "第一轮"):
        pass

    blocked = []
    async for event in chat.stream_message("user1", "testapp", conv.id, "超预算"):
        blocked.append(event)
    assert blocked[0].get("type") == "error"
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_title_middleware(tmp_path, monkeypatch):
    class FakeLLM:
        async def ask(self, prompt: str, **kwargs) -> str:
            return "戒烟计划"

    import nexus.chat.middleware.title as title_module

    monkeypatch.setattr(title_module, "get_llm_service", lambda: FakeLLM())
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'title.db'}")
    store = LocalChatStore(db_engine)
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), TitleMiddleware(store)],
    )
    chat.register("testapp", FakeHandler())
    conv = await chat.create_conversation("user1", "testapp")
    assert conv.title == "测试对话"
    await chat.send_message("user1", "testapp", conv.id, "我想戒烟")
    got = await chat.get_conversation("user1", conv.id)
    assert got.title == "戒烟计划"
    await db_engine.dispose()


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
