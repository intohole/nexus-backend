"""记忆中间件 — 每轮召回注入 context_parts，回合结束回写会话历史到 beeMemory。

旁路增强类中间件：beeMemory 不可用/异常一律降级为 no-op，绝不阻断聊天主链。
注入走 context_parts（经 BaseChatHandler._build_system 拼进 system）；
回写用 after 拿全量回复拼 messages，channel 透传供 E2E 流量隔离（channel=e2e）。
"""
from __future__ import annotations

import asyncio
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
        self._bg_tasks: set[asyncio.Task[None]] = set()

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
        # 回写后台化：beeMemory 挂起时同步 await 会把 done 帧卡住 10-15s
        # （每轮对话尾部延迟）。后台任务自带短超时+终态日志（S51 三件套）。
        task = asyncio.create_task(self._save_history_bg(context, user_id, messages))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _save_history_bg(self, context: ChatContext, user_id: str, messages: list) -> None:
        try:
            await asyncio.wait_for(
                self._sdk.save_history(
                    user_id=user_id,
                    messages=messages,
                    session_id=str(context.conversation.id),
                    channel=self._channel,
                ),
                timeout=5.0,
            )
        except Exception as exc:
            logger.warning("memory save_history failed (bg): %s", exc)
