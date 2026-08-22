from __future__ import annotations

from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.chat.store import ChatStore


class HistoryMiddleware(BaseChatMiddleware):
    def __init__(self, store: ChatStore, max_turns: int = 10) -> None:
        self._store = store
        self._max_turns = max_turns

    async def before(self, context: ChatContext) -> None:
        messages = await self._store.recent_messages(context.conversation.id, self._max_turns * 2)
        context.history_messages = [{"role": m.role, "content": m.content} for m in messages]
