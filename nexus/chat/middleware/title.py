"""标题中间件 — 首轮对话自动生成会话标题。"""

from __future__ import annotations

from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.chat.store import ChatStore
from nexus.llm import get_llm_service


class TitleMiddleware(BaseChatMiddleware):
    def __init__(self, store: ChatStore, max_length: int = 20) -> None:
        self._store = store
        self._max_length = max_length

    async def after(self, context: ChatContext, reply: str) -> None:
        if context.conversation.title != context.default_title:
            return
        count = await self._store.count_messages(context.conversation.id)
        if count > 2:
            return
        title = await self._generate_title(context.user_message)
        if title:
            await self._store.update_conversation(context.conversation.id, title=title)

    async def _generate_title(self, user_message: str) -> str:
        try:
            prompt = f"用不超过{self._max_length}个字概括这段对话的主题，只输出标题：\n{user_message}"
            title = await get_llm_service().ask(
                prompt,
                system="你是标题生成器，只输出简短标题，不要标点符号和引号。",
                temperature=0.2,
                max_tokens=30,
            )
            return title.strip()[: self._max_length]
        except Exception:
            return ""
