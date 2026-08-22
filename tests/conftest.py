from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from nexus.chat import BaseChatHandler, ChatEngine, HistoryMiddleware, LocalChatStore
from nexus.chat.context import ChatContext


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


def build_chat(path: str, middlewares, handler: BaseChatHandler | None = None):
    db_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    store = LocalChatStore(db_engine)
    mws = middlewares(store) if callable(middlewares) else middlewares
    chat = ChatEngine(engine=db_engine, store=store, middlewares=mws)
    chat.register("testapp", handler or FakeHandler())
    return db_engine, store, chat


@pytest_asyncio.fixture
async def engine(tmp_path):
    db_engine, store, chat = build_chat(str(tmp_path / "test.db"), lambda s: [HistoryMiddleware(s)])
    yield chat
    await db_engine.dispose()