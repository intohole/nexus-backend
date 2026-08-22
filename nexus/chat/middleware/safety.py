"""安全过滤中间件 — 输入内容拦截与输出内容清洗, 防敏感/违规内容进入或流出 LLM。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from nexus.chat.context import ChatContext
from nexus.chat.middleware.base import BaseChatMiddleware
from nexus.errors import ContentFilterError


class SafetyMiddleware(BaseChatMiddleware):
    def __init__(
        self,
        forbidden_words: list[str] | None = None,
        mask_token: str = "*",
        report_hits: bool = True,
    ) -> None:
        self._forbidden = tuple(forbidden_words or [])
        self._mask = mask_token
        self._report_hits = report_hits

    async def before(self, context: ChatContext) -> None:
        if not self._forbidden:
            return
        hit = self._match(context.user_message)
        if hit:
            raise ContentFilterError(
                "输入包含敏感内容, 请调整后重试",
                details={"word": hit} if self._report_hits else {},
            )

    async def wrap_stream(
        self, context: ChatContext, stream: AsyncIterator[str | dict]
    ) -> AsyncIterator[str | dict]:
        if not self._forbidden:
            async for chunk in stream:
                yield chunk
            return
        async for chunk in stream:
            if isinstance(chunk, dict):
                if chunk.get("type") in ("delta", "thinking") and chunk.get("content"):
                    chunk = {**chunk, "content": self._sanitize(str(chunk["content"]))}
                yield chunk
            else:
                yield self._sanitize(str(chunk))

    async def after(self, context: ChatContext, reply: str) -> None:
        return None

    def _match(self, text: str) -> str | None:
        lowered = text.lower()
        for word in self._forbidden:
            if word in lowered:
                return word
        return None

    def _sanitize(self, text: str) -> str:
        result = text
        lowered_result = result.lower()
        for word in self._forbidden:
            lowered_word = word.lower()
            idx = lowered_result.find(lowered_word)
            while idx != -1:
                result = result[:idx] + self._mask * len(lowered_word) + result[idx + len(lowered_word):]
                lowered_result = lowered_result[:idx] + self._mask * len(lowered_word) + lowered_result[idx + len(lowered_word):]
                idx = lowered_result.find(lowered_word, idx + len(lowered_word))
        return result