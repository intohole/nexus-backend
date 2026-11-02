"""MemoryMiddleware 守底：注入 context_parts、回写 save_history、失败静默、guest 短路。"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.chat import ChatEngine, HistoryMiddleware, LocalChatStore
from nexus.chat.context import ChatContext
from nexus.chat.middleware.memory import MemoryMiddleware
from nexus.chat.models import ChatConversation

RECALL_OK = {
    "memories": [
        {"id": "m1", "content": "用户对花生过敏", "source_app": "aipet", "created_at": "2026-10-28T09:30:00"}
    ],
    "count": 1,
    "analysis": {"trigger": "direct"},
}


class FakeSDK:
    def __init__(self, result=None, error=False):
        self.result = result if result is not None else dict(RECALL_OK)
        self.error = error
        self.recall_calls = []
        self.save_calls = []

    async def recall_memories(self, **kwargs):
        self.recall_calls.append(kwargs)
        if self.error:
            raise RuntimeError("boom")
        return self.result

    async def save_history(self, **kwargs):
        self.save_calls.append(kwargs)
        return {"success": True}


def build_context(user_id="u1", message="我能吃什么"):
    conversation = ChatConversation(user_id=user_id, app_name="testapp")
    return ChatContext(conversation=conversation, user_message=message)


def test_before_injects_context_part():
    sdk = FakeSDK()
    middleware = MemoryMiddleware(sdk, top_k=4, max_chars=2000)
    context = build_context()
    asyncio.run(middleware.before(context))
    assert len(context.context_parts) == 1
    assert "用户对花生过敏" in context.context_parts[0]
    assert sdk.recall_calls[0]["user_id"] == "u1"
    assert sdk.recall_calls[0]["max_chars"] == 2000


def test_before_guest_and_blank_short_circuit():
    sdk = FakeSDK()
    middleware = MemoryMiddleware(sdk)
    asyncio.run(middleware.before(build_context(user_id="guest")))
    asyncio.run(middleware.before(build_context(message="  ")))
    assert context_free(sdk)


def context_free(sdk):
    assert sdk.recall_calls == []
    return True


def test_before_recall_failure_is_silent():
    sdk = FakeSDK(error=True)
    middleware = MemoryMiddleware(sdk)
    context = build_context()
    asyncio.run(middleware.before(context))
    assert context.context_parts == []


def test_after_saves_history_with_channel():
    sdk = FakeSDK()
    middleware = MemoryMiddleware(sdk, channel="e2e")
    context = build_context()
    context.history_messages = [{"role": "user", "content": "上次问了什么"}]
    asyncio.run(middleware.after(context, "你问过旅行规划"))
    assert len(sdk.save_calls) == 1
    call = sdk.save_calls[0]
    assert call["channel"] == "e2e"
    assert call["user_id"] == "u1"
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["user", "user", "assistant"]
    assert call["messages"][-1]["content"] == "你问过旅行规划"


def test_after_skips_guest_and_empty_reply():
    sdk = FakeSDK()
    middleware = MemoryMiddleware(sdk)
    asyncio.run(middleware.after(build_context(user_id="guest"), "回复"))
    asyncio.run(middleware.after(build_context(), "  "))
    assert sdk.save_calls == []


@pytest.mark.asyncio
async def test_engine_pipeline_injects_memory(tmp_path):
    from nexus.chat import BaseChatHandler

    captured: list = []

    class Handler(BaseChatHandler):
        default_title = "测试对话"

        async def stream_reply(self, context, messages):
            captured.append(context)
            yield "好的"

    db_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mem.db'}")
    store = LocalChatStore(db_engine)
    sdk = FakeSDK()
    chat = ChatEngine(
        engine=db_engine,
        store=store,
        middlewares=[HistoryMiddleware(store), MemoryMiddleware(sdk)],
    )
    chat.register("testapp", Handler())
    conversation = await chat.create_conversation("u1", "testapp")
    chunks = []
    async for chunk in chat.stream_message("u1", "testapp", conversation.id, "我能吃什么"):
        chunks.append(chunk)
    assert any(isinstance(c, dict) and c.get("type") == "done" for c in chunks)
    assert "用户对花生过敏" in "".join(captured[0].context_parts)
    await db_engine.dispose()
