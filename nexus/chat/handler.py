"""聊天业务处理器协议 — 项目仅需实现 ChatHandler 即可接入统一引擎。

业务差异(上下文构建/系统提示/回复生成)留在项目侧, 通用编排由引擎承担。
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from nexus.chat.context import ChatContext
from nexus.chat.models import ChatConversation
from nexus.llm import get_llm_service


class ChatHandler(Protocol):
    default_title: str
    capabilities: set[str]

    async def build_context(self, context: ChatContext) -> None: ...
    def system_prompt(self, context: ChatContext) -> str | None: ...
    async def stream_reply(
        self, context: ChatContext, messages: list[dict[str, str]]
    ) -> AsyncIterator[str | dict]: ...
    async def on_reply_complete(
        self, context: ChatContext, user_message: str, reply: str
    ) -> dict | None: ...
    async def on_conversation_created(self, conversation: ChatConversation) -> None: ...
    def stream_meta(self, context: ChatContext) -> dict: ...


class BaseChatHandler:
    default_title: str = "新对话"
    capabilities: set[str] = {"chat", "stream"}

    async def build_context(self, context: ChatContext) -> None:
        return None

    def system_prompt(self, context: ChatContext) -> str | None:
        return None

    async def stream_reply(
        self, context: ChatContext, messages: list[dict[str, str]]
    ) -> AsyncIterator[str | dict]:
        async for chunk in get_llm_service().stream_chat(messages, system=self._build_system(context)):
            yield chunk

    async def on_reply_complete(
        self, context: ChatContext, user_message: str, reply: str
    ) -> dict | None:
        return None

    async def on_conversation_created(self, conversation: ChatConversation) -> None:
        return None

    def stream_meta(self, context: ChatContext) -> dict:
        return {}

    def _build_system(self, context: ChatContext) -> str | None:
        parts = list(context.context_parts)
        base = self.system_prompt(context)
        if base:
            parts.append(base)
        return "\n\n".join(parts) if parts else None
