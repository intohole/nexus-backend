from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from conftest import FakeHandler, MetaHandler, build_chat
from nexus.chat import (
    CostMiddleware,
    HistoryMiddleware,
    RateLimitMiddleware,
    SafetyMiddleware,
    TitleMiddleware,
)


@pytest.mark.asyncio
async def test_rate_limit_middleware(tmp_path):
    db_engine, store, chat = build_chat(
        str(tmp_path / "rate.db"),
        lambda s: [HistoryMiddleware(s), RateLimitMiddleware(s, max_messages=1, window_seconds=3600)],
    )
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
async def test_cost_middleware(tmp_path):
    db_engine, _, chat = build_chat(
        str(tmp_path / "cost.db"),
        lambda s: [HistoryMiddleware(s), CostMiddleware(s, input_price_per_1k=1.0, output_price_per_1k=2.0)],
    )
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "你好")
    got = await chat.get_conversation("user1", conv.id)
    stats = got.meta["cost"]
    assert stats["rounds"] == 1
    assert stats["tokens"] > 0
    assert stats["amount"] >= 0
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_meta_coexist(tmp_path):
    db_engine, _, chat = build_chat(
        str(tmp_path / "coexist.db"),
        lambda s: [HistoryMiddleware(s), CostMiddleware(s)],
        MetaHandler(),
    )
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "你好")
    got = await chat.get_conversation("user1", conv.id)
    assert got.meta["phase"] == "done"
    assert got.meta["cost"]["rounds"] == 1
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_usage_report(tmp_path):
    class UsageHandler(FakeHandler):
        async def stream_reply(self, context, messages):
            context.set_usage(prompt_tokens=100, completion_tokens=50)
            yield "回复"

    db_engine, _, chat = build_chat(
        str(tmp_path / "usage.db"),
        lambda s: [HistoryMiddleware(s), CostMiddleware(s)],
        UsageHandler(),
    )
    conv = await chat.create_conversation("user1", "testapp")
    await chat.send_message("user1", "testapp", conv.id, "hi")
    got = await chat.get_conversation("user1", conv.id)
    assert got.meta["cost"]["tokens"] == 150
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_cost_budget(tmp_path):
    db_engine, _, chat = build_chat(
        str(tmp_path / "budget.db"),
        lambda s: [HistoryMiddleware(s), CostMiddleware(s, token_budget=1)],
    )
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
    db_engine, _, chat = build_chat(
        str(tmp_path / "title.db"),
        lambda s: [HistoryMiddleware(s), TitleMiddleware(s)],
    )
    conv = await chat.create_conversation("user1", "testapp")
    assert conv.title == "测试对话"
    await chat.send_message("user1", "testapp", conv.id, "我想戒烟")
    got = await chat.get_conversation("user1", conv.id)
    assert got.title == "戒烟计划"
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_safety_blocks_input_and_sanitizes_output(tmp_path):
    class SensitiveHandler(FakeHandler):
        async def stream_reply(self, context, messages):
            yield "这里提到暴力是不对的"

    db_engine, _, chat = build_chat(
        str(tmp_path / "safety.db"),
        lambda s: [HistoryMiddleware(s), SafetyMiddleware(forbidden_words=["暴力", "slash"])],
    )
    chat.register("sensitive", SensitiveHandler())
    conv = await chat.create_conversation("user1", "sensitive")

    blocked = []
    async for event in chat.stream_message("user1", "sensitive", conv.id, "我要暴力"):
        blocked.append(event)
    assert blocked[0].get("type") == "error"
    assert blocked[0].get("code") == 422

    safe = []
    async for event in chat.stream_message("user1", "sensitive", conv.id, "普通问题"):
        safe.append(event)
    assert any(e.get("content") == "这里提到**是不对的" for e in safe if e.get("type") == "done")
    await db_engine.dispose()


@pytest.mark.asyncio
async def test_safety_case_insensitive_output(tmp_path):
    class LatinHandler(FakeHandler):
        async def stream_reply(self, context, messages):
            yield "use SLaSh rarely"

    db_engine, _, chat = build_chat(
        str(tmp_path / "safety2.db"),
        lambda s: [SafetyMiddleware(forbidden_words=["SLASH"])],
    )
    chat.register("latin", LatinHandler())
    conv = await chat.create_conversation("user1", "latin")

    events = []
    async for event in chat.stream_message("user1", "latin", conv.id, "普通问题"):
        events.append(event)
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["content"] == "use ***** rarely"
    await db_engine.dispose()