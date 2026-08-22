"""统一聊天引擎 — 编排 Handler/Middleware/Store/Transport/EventBus 五大扩展点。"""

from __future__ import annotations

from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine

from nexus.chat.context import ChatContext
from nexus.chat.events import ChatEventBus
from nexus.chat.handler import ChatHandler
from nexus.chat.middleware.base import ChatMiddleware
from nexus.chat.middleware.history import HistoryMiddleware
from nexus.chat.middleware.title import TitleMiddleware
from nexus.chat.models import ChatConversation
from nexus.chat.store import ChatStore, LocalChatStore
from nexus.chat.transport import ChatTransport, SSETransport
from nexus.errors import ForbiddenError, NotFoundError


class ChatEngine:
    def __init__(
        self,
        engine: AsyncEngine,
        store: ChatStore | None = None,
        transport: ChatTransport | None = None,
        event_bus: ChatEventBus | None = None,
        middlewares: list[ChatMiddleware] | None = None,
    ) -> None:
        self._store = store or LocalChatStore(engine)
        self._transport = transport or SSETransport()
        self._event_bus = event_bus or ChatEventBus()
        self._handlers: dict[str, ChatHandler] = {}
        self._middlewares = middlewares if middlewares is not None else [
            HistoryMiddleware(self._store),
            TitleMiddleware(self._store),
        ]

    @property
    def store(self) -> ChatStore:
        return self._store

    @property
    def transport(self) -> ChatTransport:
        return self._transport

    @property
    def event_bus(self) -> ChatEventBus:
        return self._event_bus

    def register(self, app_name: str, handler: ChatHandler) -> "ChatEngine":
        self._handlers[app_name] = handler
        return self

    def use(self, middleware: ChatMiddleware) -> "ChatEngine":
        self._middlewares.append(middleware)
        return self

    def get_handler(self, app_name: str) -> ChatHandler:
        handler = self._handlers.get(app_name)
        if handler is None:
            raise NotFoundError(f"no chat handler registered for app: {app_name}")
        return handler

    async def create_conversation(
        self, user_id: str, app_name: str, title: str | None = None
    ) -> ChatConversation:
        handler = self.get_handler(app_name)
        conversation = ChatConversation(
            user_id=user_id,
            app_name=app_name,
            title=title or handler.default_title,
        )
        conversation = await self._store.create_conversation(conversation)
        await handler.on_conversation_created(conversation)
        await self._event_bus.publish("conversation.created", {"conversation_id": conversation.id})
        return conversation

    async def list_conversations(
        self,
        user_id: str,
        app_name: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self._store.list_conversations(user_id, app_name, status, page, page_size)

    async def get_conversation(self, user_id: str, conversation_id: str) -> ChatConversation:
        conversation = await self._store.get_conversation(conversation_id)
        if conversation is None:
            raise NotFoundError("conversation not found")
        if conversation.user_id != user_id:
            raise ForbiddenError("not allowed to access this conversation")
        return conversation

    async def update_conversation(
        self, user_id: str, conversation_id: str, **fields: Any
    ) -> ChatConversation:
        await self.get_conversation(user_id, conversation_id)
        conversation = await self._store.update_conversation(conversation_id, **fields)
        if conversation is None:
            raise NotFoundError("conversation not found")
        return conversation

    async def delete_conversation(self, user_id: str, conversation_id: str) -> None:
        await self.get_conversation(user_id, conversation_id)
        await self._store.delete_conversation(conversation_id)
        await self._event_bus.publish("conversation.deleted", {"conversation_id": conversation_id})

    async def search_conversations(
        self, user_id: str, app_name: str, query: str, limit: int = 20
    ) -> list[ChatConversation]:
        return await self._store.search_conversations(user_id, app_name, query, limit)

    async def list_messages(
        self, user_id: str, conversation_id: str, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        await self.get_conversation(user_id, conversation_id)
        return await self._store.list_messages(conversation_id, page, page_size)

    async def stream_message(
        self, user_id: str, app_name: str, conversation_id: str, content: str
    ) -> AsyncIterator[str | dict]:
        handler = self.get_handler(app_name)
        conversation = await self.get_conversation(user_id, conversation_id)

        context = ChatContext(
            conversation=conversation,
            user_message=content,
            default_title=handler.default_title,
        )
        await handler.build_context(context)

        for middleware in self._middlewares:
            await middleware.before(context)

        messages = list(context.history_messages) + [{"role": "user", "content": content}]

        async def core() -> AsyncIterator[str | dict]:
            async for chunk in handler.stream_reply(context, messages):
                yield chunk

        stream: AsyncIterator[str | dict] = core()
        for middleware in reversed(self._middlewares):
            stream = middleware.wrap_stream(context, stream)

        collected: list[str] = []
        error: str | None = None
        try:
            async for chunk in stream:
                if isinstance(chunk, dict):
                    if chunk.get("type") == "delta":
                        collected.append(str(chunk.get("content", "")))
                    yield chunk
                else:
                    collected.append(str(chunk))
                    yield {"type": "delta", "content": chunk}
        except Exception as exc:
            error = str(exc)

        if error is not None:
            yield {"type": "error", "message": error}
            return

        reply = "".join(collected)
        await self._store.add_message(conversation_id, "user", content)
        assistant_message = await self._store.add_message(conversation_id, "assistant", reply)

        for middleware in self._middlewares:
            await middleware.after(context, reply)

        updated_meta = await handler.on_reply_complete(context, content, reply)
        if updated_meta:
            await self._store.update_conversation(conversation_id, meta=updated_meta)

        await self._event_bus.publish(
            "message.completed",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message.id,
                "user_id": user_id,
                "app_name": app_name,
            },
        )

        yield {"type": "done", "content": reply, "message_id": assistant_message.id}

    async def send_message(
        self, user_id: str, app_name: str, conversation_id: str, content: str
    ) -> dict[str, Any]:
        reply: str = ""
        message_id: str | None = None
        async for event in self.stream_message(user_id, app_name, conversation_id, content):
            if isinstance(event, dict):
                if event.get("type") == "delta":
                    reply += str(event.get("content", ""))
                elif event.get("type") == "done":
                    message_id = event.get("message_id")
        return {"content": reply, "message_id": message_id}
