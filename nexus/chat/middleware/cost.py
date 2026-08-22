"""成本追踪中间件 — 累计每轮 LLM 用量并估算成本, 持久化到会话 meta。"""

from __future__ import annotations

from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.chat.store import ChatStore


def _estimate_tokens(text: str) -> int:
    return len(text) // 2 + 1 if text else 0


class CostMiddleware(BaseChatMiddleware):
    def __init__(
        self,
        store: ChatStore,
        token_budget: int | None = None,
        input_price_per_1k: float = 0.0,
        output_price_per_1k: float = 0.0,
    ) -> None:
        self._store = store
        self._token_budget = token_budget
        self._input_price = input_price_per_1k
        self._output_price = output_price_per_1k

    async def before(self, context: ChatContext) -> None:
        if self._token_budget is None:
            return
        total = await self._conversation_tokens(context.conversation.id)
        if total >= self._token_budget:
            raise PermissionError("会话用量已超预算, 请开启新会话继续")

    async def after(self, context: ChatContext, reply: str) -> None:
        usage = context.usage
        prompt_tokens = usage.get("prompt_tokens") or _estimate_tokens(context.user_message)
        completion_tokens = usage.get("completion_tokens") or _estimate_tokens(reply)
        cost = (
            prompt_tokens * self._input_price / 1000
            + completion_tokens * self._output_price / 1000
        )
        meta = dict(context.conversation.meta or {})
        stats = meta.setdefault("cost", {})
        stats["tokens"] = int(stats.get("tokens", 0)) + prompt_tokens + completion_tokens
        stats["amount"] = round(float(stats.get("amount", 0)) + cost, 6)
        stats["rounds"] = int(stats.get("rounds", 0)) + 1
        await self._store.update_conversation(context.conversation.id, meta=meta)
        context.conversation.meta = meta

    async def _conversation_tokens(self, conversation_id: str) -> int:
        conversation = await self._store.get_conversation(conversation_id)
        if conversation is None:
            return 0
        stats = (conversation.meta or {}).get("cost", {})
        return int(stats.get("tokens", 0))