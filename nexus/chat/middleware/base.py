from __future__ import annotations

from typing import AsyncIterator, Protocol

from nexus.chat.context import ChatContext


class ChatMiddleware(Protocol):
    async def before(self, context: ChatContext) -> None: ...
    async def after(self, context: ChatContext, reply: str) -> None: ...
    async def wrap_stream(
        self, context: ChatContext, stream: AsyncIterator[str | dict]
    ) -> AsyncIterator[str | dict]: ...


class BaseChatMiddleware:
    async def before(self, context: ChatContext) -> None:
        return None

    async def after(self, context: ChatContext, reply: str) -> None:
        return None

    async def wrap_stream(
        self, context: ChatContext, stream: AsyncIterator[str | dict]
    ) -> AsyncIterator[str | dict]:
        async for chunk in stream:
            yield chunk
