"""记忆中间件 — 每轮召回注入 context_parts，回合结束回写会话历史到 beeMemory。

旁路增强类中间件：beeMemory 不可用/异常一律降级为 no-op，绝不阻断聊天主链。
注入走 context_parts（经 BaseChatHandler._build_system 拼进 system）；
回写用 after 拿全量回复拼 messages，channel 透传供 E2E 流量隔离（channel=e2e）。
"""
from __future__ import annotations

import logging
from typing import Optional

from nexus.bm_sdk import BeeMemorySDK
from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.memory import render_memory_context

logger = logging.getLogger("nexus.chat.memory")

_GUEST_IDS = ("", "guest", "anonymous")


class MemoryMiddleware(BaseChatMiddleware):
    def __init__(
        self,
        sdk: BeeMemorySDK,
        *,
        top_k: int = 4,
        max_chars: int = 2000,
        channel: Optional[str] = None,
        smart: bool = True,
    ) -> None:
        self._sdk = sdk
        self._top_k = top_k
        self._max_chars = max_chars
        self._channel = channel
        self._smart = smart

    async def before(self, context: ChatContext) -> None:
        user_id = str(context.conversation.user_id or "").strip()
        if user_id.lower() in _GUEST_IDS or not context.user_message.strip():
            return
        try:
            result = await self._sdk.recall_memories(
                content=context.user_message,
                user_id=user_id,
                top_k=self._top_k,
                max_chars=self._max_chars,
                smart=self._smart,
            )
        except Exception as exc:
            logger.warning("memory recall failed, skip injection: %s", exc)
            return
        block = render_memory_context(result, kind="recall")
        if block:
            context.context_parts.append(block)

    async def after(self, context: ChatContext, reply: str) -> None:
        user_id = str(context.conversation.user_id or "").strip()
        if user_id.lower() in _GUEST_IDS or not reply.strip():
            return
        messages = list(context.history_messages) + [
            {"role": "user", "content": context.user_message},
            {"role": "assistant", "content": reply},
        ]
        try:
            await self._sdk.save_history(
                user_id=user_id,
                messages=messages,
                session_id=str(context.conversation.id),
                channel=self._channel,
            )
        except Exception as exc:
            logger.warning("memory save_history failed: %s", exc)
