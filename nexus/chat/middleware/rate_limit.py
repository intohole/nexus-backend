"""限流中间件 — 会话级消息频率治理, 防止单会话过量消耗 LLM 配额。"""

from __future__ import annotations

from datetime import timedelta

from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.chat.models import _utcnow
from nexus.chat.store import ChatStore
from nexus.errors import RateLimitError


class RateLimitMiddleware(BaseChatMiddleware):
    def __init__(self, store: ChatStore, max_messages: int = 30, window_seconds: int = 3600) -> None:
        self._store = store
        self._max_messages = max_messages
        self._window_seconds = window_seconds

    async def before(self, context: ChatContext) -> None:
        cutoff = _utcnow() - timedelta(seconds=self._window_seconds)
        count = await self._store.count_messages_since(context.conversation.id, cutoff)
        if count >= self._max_messages:
            raise RateLimitError(
                f"会话发送过于频繁(近{self._window_seconds // 60}分钟内已{self._max_messages}条), 请稍后再试",
                details={"max_messages": self._max_messages, "window_seconds": self._window_seconds},
            )