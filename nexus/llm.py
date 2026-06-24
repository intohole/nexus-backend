from __future__ import annotations

import asyncio
import os
from typing import Optional

from nexus.logging import get_logger
from nexus.llm_utils import parse_llm_json, with_retry, LLMTimeoutError, strip_code_fence

logger = get_logger("nexus.llm")

_ironman_configured: bool = False
_ironman_lock: asyncio.Lock = asyncio.Lock()


async def configure_ironman(yaml_path: Optional[str] = None) -> None:
    global _ironman_configured
    if _ironman_configured:
        return

    async with _ironman_lock:
        if _ironman_configured:
            return

        import ironman

        path = yaml_path or os.environ.get("IRONMAN_CONFIG", "")
        if path and os.path.exists(path):
            await ironman.configure(config_path=path)
            _ironman_configured = True
            logger.info("Ironman configured from %s", path)
        else:
            logger.warning("Ironman config not found, using env vars")


class LLMService:
    _instance: Optional["LLMService"] = None

    def __new__(cls) -> "LLMService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> str:
        await configure_ironman()
        from ironman import chat as _chat
        from ironman.types import LLMOptions, Message, Role

        ironman_messages: list[Message] = []
        if system:
            ironman_messages.append(Message(role=Role.SYSTEM, content=system))
        for msg in messages:
            role_str = msg.get("role", "user")
            content = msg.get("content", "")
            if role_str == "user":
                ironman_messages.append(Message(role=Role.USER, content=content))
            elif role_str == "assistant":
                ironman_messages.append(Message(role=Role.ASSISTANT, content=content))
            elif role_str == "system":
                ironman_messages.append(Message(role=Role.SYSTEM, content=content))

        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)

        async def _do() -> str:
            response = await _chat(messages=ironman_messages, llm=llm_opts)
            return response.content

        return await with_retry(_do, timeout, max_retries)

    async def ask(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> str:
        await configure_ironman()
        from ironman import ask as _ask
        from ironman.types import LLMOptions

        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)

        async def _do() -> str:
            return await _ask(prompt=prompt, system=system, llm=llm_opts)

        return await with_retry(_do, timeout, max_retries)

    async def ask_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 1500,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> dict[str, object]:
        raw = await self.ask(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
        )
        return parse_llm_json(raw)

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 1500,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> dict[str, object]:
        raw = await self.chat(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
        )
        return parse_llm_json(raw)

    async def extract(
        self,
        prompt: str,
        schema: Optional[type] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> Optional[object]:
        await configure_ironman()
        from ironman import extract as _extract
        from ironman.types import LLMOptions

        async def _do() -> object:
            return await _extract(
                prompt=prompt,
                schema=schema,
                llm=LLMOptions(),
                max_retries=0,
            )

        try:
            return await with_retry(_do, timeout, max_retries)
        except Exception as e:
            logger.error("Extraction failed: %s", e)
            return None


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
