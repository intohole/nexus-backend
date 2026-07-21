from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncGenerator, Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.llm_metrics import get_llm_metrics
from nexus.circuit_breaker import get_llm_circuit
from nexus.llm_utils import parse_llm_json, with_retry
from nexus.llm_cache import get_prompt_cache, PromptCache

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
            logger.info("Ironman configured from %s", path)
        else:
            logger.warning("Ironman config not found, assuming externally configured")
        _ironman_configured = True


def mark_ironman_configured() -> None:
    global _ironman_configured
    _ironman_configured = True


def _effective_retries(max_retries: int) -> int:
    """Gateway mode reduces retries to 1 (gateway already has 3x failover)."""
    try:
        from nexus.ironman import is_gateway_mode
        if is_gateway_mode():
            return 1
    except ImportError:
        pass
    return max_retries


def _resolve_app_name() -> str:
    try:
        from nexus.ironman import get_init_app_name
        name: Optional[str] = get_init_app_name()
        if name:
            return name
    except ImportError:
        pass
    return os.environ.get("APP_NAME", "unknown")


class LLMService:
    _instance: Optional["LLMService"] = None

    def __new__(cls) -> "LLMService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _convert_messages(messages: list[dict[str, str]], system: Optional[str]) -> list:
        from ironman.types import Message, Role
        out: list = []
        if system:
            out.append(Message(role=Role.SYSTEM, content=system))
        for msg in messages:
            role_str = msg.get("role", "user")
            content = msg.get("content", "")
            role = {"user": Role.USER, "assistant": Role.ASSISTANT, "system": Role.SYSTEM}.get(role_str, Role.USER)
            out.append(Message(role=role, content=content))
        return out

    @staticmethod
    def _extract_content(response: object, request_id: str = "-") -> str:
        if response.content:
            return response.content
        if getattr(response, "reasoning", None):
            logger.warning("LLM empty content, using reasoning fallback [req_id=%s]", request_id)
            return response.reasoning
        logger.warning("LLM empty content and no reasoning [req_id=%s]", request_id)
        return ""

    async def _resilient_chat(
        self, op: str, messages: list, llm_opts, request_id: str, app_name: str,
        timeout: float, max_retries: int,
    ) -> str:
        """Shared circuit + retry + metrics + logging wrapper for chat/ask."""
        from ironman import chat as _chat

        async def _do() -> str:
            response = await _chat(messages=messages, llm=llm_opts)
            return self._extract_content(response, request_id)

        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            result: str = await with_retry(
                lambda: circuit.call(_do), timeout, _effective_retries(max_retries)
            )
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=None)
            logger.info("LLM %s ok [req_id=%s, app=%s, %.2fs]", op, request_id, app_name, latency)
            return result
        except Exception as e:
            latency = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=type(e).__name__)
            logger.error("LLM %s fail [req_id=%s, app=%s, %.2fs]: %s", op, request_id, app_name, latency, e)
            raise

    def _cache_lookup(self, key: Optional[str], request_id: str, app_name: str) -> Optional[str]:
        if key is None:
            return None
        cached: Optional[str] = get_prompt_cache().get(key)
        if cached is not None:
            logger.info("LLM cache hit [req_id=%s, app=%s]", request_id, app_name)
        return cached

    def _cache_store(self, key: Optional[str], value: str) -> None:
        if key is not None and value:
            get_prompt_cache().set(key, value)

    async def chat(
        self, messages: list[dict[str, str]], system: Optional[str] = None,
        temperature: float = 0.7, max_tokens: Optional[int] = None, timeout: float = 60.0,
        max_retries: int = 3, json_mode: bool = False,
    ) -> str:
        await configure_ironman()
        from ironman.types import LLMOptions
        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        extra: dict[str, object] | None = {"response_format": {"type": "json_object"}} if json_mode else None
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens, extra=extra)
        cache_key: Optional[str] = None
        if temperature == 0:
            cache_key = PromptCache.make_messages_key(system, messages, temperature, max_tokens)
            cached = self._cache_lookup(cache_key, request_id, app_name)
            if cached is not None:
                return cached
        ironman_messages = self._convert_messages(messages, system)
        result = await self._resilient_chat("chat", ironman_messages, llm_opts, request_id, app_name, timeout, max_retries)
        self._cache_store(cache_key, result)
        return result

    async def ask(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.7,
        max_tokens: Optional[int] = None, timeout: float = 60.0, max_retries: int = 3,
        json_mode: bool = False,
    ) -> str:
        await configure_ironman()
        from ironman.types import LLMOptions, Message, Role
        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        extra: dict[str, object] | None = {"response_format": {"type": "json_object"}} if json_mode else None
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens, extra=extra)
        cache_key: Optional[str] = None
        if temperature == 0:
            cache_key = PromptCache.make_key(system, prompt, temperature, max_tokens)
            cached = self._cache_lookup(cache_key, request_id, app_name)
            if cached is not None:
                return cached
        msgs: list = []
        if system:
            msgs.append(Message(role=Role.SYSTEM, content=system))
        msgs.append(Message(role=Role.USER, content=prompt))
        result = await self._resilient_chat("ask", msgs, llm_opts, request_id, app_name, timeout, max_retries)
        self._cache_store(cache_key, result)
        return result

    async def ask_json(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.2,
        max_tokens: Optional[int] = 1500, timeout: float = 60.0, max_retries: int = 3,
    ) -> dict[str, object]:
        return parse_llm_json(await self.ask(prompt, system, temperature, max_tokens, timeout, max_retries))

    async def chat_json(
        self, messages: list[dict[str, str]], system: Optional[str] = None,
        temperature: float = 0.2, max_tokens: Optional[int] = 1500, timeout: float = 60.0,
        max_retries: int = 3,
    ) -> dict[str, object]:
        return parse_llm_json(await self.chat(messages, system, temperature, max_tokens, timeout, max_retries))

    async def extract(
        self, prompt: str, schema: Optional[type] = None, timeout: float = 60.0,
        max_retries: int = 3, raise_on_error: bool = False,
    ) -> Optional[object]:
        await configure_ironman()
        from ironman import extract as _extract
        from ironman.types import LLMOptions
        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()

        async def _do() -> object:
            return await _extract(prompt=prompt, schema=schema, llm=LLMOptions())

        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            result: object = await with_retry(
                lambda: circuit.call(_do), timeout, _effective_retries(max_retries)
            )
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=None)
            logger.info("LLM extract ok [req_id=%s, app=%s, %.2fs]", request_id, app_name, latency)
            return result
        except Exception as e:
            latency = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=type(e).__name__)
            if type(e).__name__ == "CircuitBreakerOpenError":
                logger.warning("LLM extract blocked by open circuit [req_id=%s, app=%s]: %s", request_id, app_name, e)
                raise
            logger.error("LLM extract fail [req_id=%s, app=%s, %.2fs]: %s", request_id, app_name, latency, e)
            if raise_on_error:
                raise
            return None

    async def _stream(
        self, op: str, messages: list, llm_opts, request_id: str,
    ) -> AsyncGenerator[str, None]:
        from ironman import chat_stream as _chat_stream
        has_content: bool = False
        reasoning_buffer: list[str] = []
        async for chunk in _chat_stream(messages=messages, llm=llm_opts):
            if chunk.content:
                has_content = True
                yield chunk.content
            elif chunk.reasoning:
                reasoning_buffer.append(chunk.reasoning)
        if not has_content and reasoning_buffer:
            logger.warning("LLM %s no content, yielding reasoning fallback [req_id=%s]", op, request_id)
            yield "".join(reasoning_buffer)

    async def stream_chat(
        self, messages: list[dict[str, str]], system: Optional[str] = None,
        temperature: float = 0.7, max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        await configure_ironman()
        from ironman.types import LLMOptions
        request_id: str = get_request_id() or "-"
        ironman_messages = self._convert_messages(messages, system)
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)
        async for chunk in self._stream("stream_chat", ironman_messages, llm_opts, request_id):
            yield chunk

    async def stream_ask(
        self, prompt: str, system: Optional[str] = None, temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        await configure_ironman()
        from ironman.types import LLMOptions, Message, Role
        request_id: str = get_request_id() or "-"
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)
        msgs: list = []
        if system:
            msgs.append(Message(role=Role.SYSTEM, content=system))
        msgs.append(Message(role=Role.USER, content=prompt))
        async for chunk in self._stream("stream_ask", msgs, llm_opts, request_id):
            yield chunk

    async def embed(
        self, texts: list[str], timeout: float = 60.0, max_retries: int = 3,
        raise_on_error: bool = False,
    ) -> Optional[list[list[float]]]:
        await configure_ironman()
        from ironman import embed as _embed
        try:
            return await with_retry(lambda: _embed(text=texts), timeout, max_retries)
        except Exception as e:
            logger.error("Embed failed: %s", e)
            if raise_on_error:
                raise
            return None


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
