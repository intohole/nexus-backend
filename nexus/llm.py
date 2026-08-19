"""LLM 统一接入层: 网关路由、确定性prompt缓存、异步调用与重试."""
from __future__ import annotations

import os
import time
from typing import AsyncGenerator, Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.llm_metrics import get_llm_metrics
from nexus.circuit_breaker import get_llm_circuit
from nexus.llm_utils import parse_llm_json, with_retry, LLMTimeoutError
from nexus.llm_helpers import (
    apply_output_discipline,
    convert_messages,
    extract_content,
    record_usage,
    resolve_namespace,
)
from nexus.llm_budget import OutputMode, TASK_BUDGETS
from nexus.llm_cache import PromptCache, get_prompt_cache
from nexus.llm_config import (
    configure_ironman,
    mark_ironman_configured,
    _effective_retries,
    _resolve_app_name,
)

logger = get_logger("nexus.llm")

DEFAULT_MAX_OUTPUT_TOKENS: int = int(os.environ.get("LLM_DEFAULT_MAX_OUTPUT_TOKENS", "2048"))


def _resolve_budget(
    task_type: Optional[str],
    max_tokens: Optional[int],
    temperature: float,
    output_mode: Optional[OutputMode],
) -> tuple[Optional[int], float, Optional[OutputMode]]:
    if not task_type:
        return max_tokens, temperature, output_mode
    budget = TASK_BUDGETS.get(task_type)
    if budget is None:
        return max_tokens, temperature, output_mode
    resolved_max = max_tokens if max_tokens is not None else budget.max_tokens
    resolved_temp = temperature if temperature is not None else (budget.temperature or 0.7)
    resolved_mode = output_mode if output_mode is not None else budget.output_mode
    return resolved_max, resolved_temp, resolved_mode


class LLMService:
    _instance: Optional["LLMService"] = None

    def __new__(cls) -> "LLMService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @staticmethod
    def _build_extra(
        json_mode: bool,
        namespace: Optional[str],
        task_type: Optional[str],
    ) -> dict[str, object] | None:
        extra: dict[str, object] | None = None
        if json_mode:
            extra = {"response_format": {"type": "json_object"}}
        ns = resolve_namespace(namespace)
        if ns:
            extra = dict(extra or {})
            extra["namespace"] = ns
        if task_type:
            extra = dict(extra or {})
            extra["task_type"] = task_type
        return extra

    async def _execute(
        self,
        do,
        timeout: float,
        max_retries: int,
        app_name: str,
        request_id: str,
        kind: str,
    ) -> str:
        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            async def _do_with_circuit() -> object:
                return await circuit.call(do)
            response: object = await with_retry(
                _do_with_circuit, timeout, _effective_retries(max_retries)
            )
            result: str = extract_content(response, request_id)
            record_usage(metrics, app_name, response, time.monotonic() - start, None)
            logger.info(
                "LLM %s completed [req_id=%s, app=%s, latency=%.2fs]",
                kind, request_id, app_name, time.monotonic() - start,
            )
            return result
        except Exception as e:
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=type(e).__name__)
            logger.error(
                "LLM %s failed [req_id=%s, app=%s, latency=%.2fs]: %s",
                kind, request_id, app_name, latency, e,
            )
            raise

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        json_mode: bool = False,
        concise: bool = False,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> str:
        await configure_ironman()
        from ironman import chat as _chat
        from ironman.types import LLMOptions, Message, Role

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        budget_max, budget_temp, budget_mode = _resolve_budget(
            task_type, max_tokens, temperature, output_mode
        )
        temperature = 0.7 if budget_temp is None else budget_temp
        eff_max_tokens: Optional[int] = (
            budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        )
        system, _ = apply_output_discipline(
            system, "", concise, json_mode, budget_mode
        )
        ironman_messages = convert_messages(messages, system)
        cache = get_prompt_cache() if temperature <= 0.0 else None
        if cache is not None:
            key: str = PromptCache.make_messages_key(system, messages, temperature, eff_max_tokens)
            hit: Optional[str] = cache.get(key)
            if hit is not None:
                return hit
        llm_opts = LLMOptions(
            temperature=temperature,
            max_tokens=eff_max_tokens,
            extra=self._build_extra(json_mode, namespace, task_type),
        )

        async def _do() -> object:
            return await _chat(messages=ironman_messages, llm=llm_opts)

        result = await self._execute(_do, timeout, max_retries, app_name, request_id, "chat")
        if cache is not None and result:
            cache.set(key, result)
        return result

    async def ask(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        json_mode: bool = False,
        concise: bool = False,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> str:
        await configure_ironman()
        from ironman import chat as _chat
        from ironman.types import LLMOptions, Message, Role

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        budget_max, budget_temp, budget_mode = _resolve_budget(
            task_type, max_tokens, temperature, output_mode
        )
        temperature = 0.7 if budget_temp is None else budget_temp
        eff_max_tokens: Optional[int] = (
            budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        )
        system, prompt = apply_output_discipline(
            system, prompt, concise, json_mode, budget_mode
        )
        cache = get_prompt_cache() if temperature <= 0.0 else None
        if cache is not None:
            key: str = PromptCache.make_key(system, prompt, temperature, eff_max_tokens)
            hit: Optional[str] = cache.get(key)
            if hit is not None:
                return hit
        llm_opts = LLMOptions(
            temperature=temperature,
            max_tokens=eff_max_tokens,
            extra=self._build_extra(json_mode, namespace, task_type),
        )

        msgs: list = []
        if system:
            msgs.append(Message(role=Role.SYSTEM, content=system))
        msgs.append(Message(role=Role.USER, content=prompt))

        async def _do() -> object:
            return await _chat(messages=msgs, llm=llm_opts)

        result = await self._execute(_do, timeout, max_retries, app_name, request_id, "ask")
        if cache is not None and result:
            cache.set(key, result)
        return result

    async def ask_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 1500,
        timeout: float = 60.0,
        max_retries: int = 3,
        concise: bool = False,
        task_type: Optional[str] = None,
    ) -> dict[str, object]:
        raw = await self.ask(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            json_mode=True,
            concise=concise,
            task_type=task_type,
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
        concise: bool = False,
        task_type: Optional[str] = None,
    ) -> dict[str, object]:
        raw = await self.chat(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
            json_mode=True,
            concise=concise,
            task_type=task_type,
        )
        return parse_llm_json(raw)

    async def extract(
        self,
        prompt: str,
        schema: Optional[type] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
        raise_on_error: bool = False,
    ) -> Optional[object]:
        await configure_ironman()
        from ironman import extract as _extract
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()

        async def _do() -> object:
            return await _extract(
                prompt=prompt,
                schema=schema,
                llm=LLMOptions(),
            )

        try:
            async def _do_with_circuit() -> object:
                return await circuit.call(_do)
            result: object = await with_retry(
                _do_with_circuit, timeout, _effective_retries(max_retries)
            )
            metrics.record(app_name, "unknown", time.monotonic() - start, tokens=0, error=None)
            logger.info(
                "LLM extract completed [req_id=%s, app=%s, latency=%.2fs]",
                request_id, app_name, time.monotonic() - start,
            )
            return result
        except Exception as e:
            latency: float = time.monotonic() - start
            error_type: str = type(e).__name__
            metrics.record(app_name, "unknown", latency, tokens=0, error=error_type)
            if error_type == "CircuitBreakerOpenError":
                logger.warning(
                    "LLM extract blocked by open circuit [req_id=%s, app=%s, latency=%.2fs]: %s",
                    request_id, app_name, latency, e,
                )
                raise
            logger.error(
                "LLM extract failed [req_id=%s, app=%s, latency=%.2fs]: %s",
                request_id, app_name, latency, e,
            )
            if raise_on_error:
                raise
            return None

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        await configure_ironman()
        from ironman import chat_stream as _chat_stream
        from ironman.types import LLMOptions

        budget_max, budget_temp, budget_mode = _resolve_budget(
            task_type, max_tokens, temperature, output_mode
        )
        temperature = 0.7 if budget_temp is None else budget_temp
        eff_max_tokens: Optional[int] = (
            budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        )
        system, _ = apply_output_discipline(system, "", False, False, budget_mode)
        ironman_messages = convert_messages(messages, system)
        llm_opts = LLMOptions(
            temperature=temperature,
            max_tokens=eff_max_tokens,
            extra=self._build_extra(False, namespace, task_type),
        )
        async for chunk in self._stream(_chat_stream, ironman_messages, llm_opts):
            yield chunk

    async def stream_ask(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        await configure_ironman()
        from ironman import chat_stream as _chat_stream
        from ironman.types import LLMOptions, Message, Role

        budget_max, budget_temp, budget_mode = _resolve_budget(
            task_type, max_tokens, temperature, output_mode
        )
        temperature = 0.7 if budget_temp is None else budget_temp
        eff_max_tokens: Optional[int] = (
            budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        )
        system, prompt = apply_output_discipline(system, prompt, False, False, budget_mode)
        llm_opts = LLMOptions(
            temperature=temperature,
            max_tokens=eff_max_tokens,
            extra=self._build_extra(False, namespace, task_type),
        )
        msgs: list = []
        if system:
            msgs.append(Message(role=Role.SYSTEM, content=system))
        msgs.append(Message(role=Role.USER, content=prompt))
        async for chunk in self._stream(_chat_stream, msgs, llm_opts):
            yield chunk

    async def _stream(
        self,
        chat_stream,
        msgs: list,
        llm_opts: object,
    ) -> AsyncGenerator[str, None]:
        metrics = get_llm_metrics()
        app_name: str = _resolve_app_name()
        start: float = time.monotonic()
        has_content: bool = False
        reasoning_buffer: list[str] = []
        last_usage: Optional[object] = None
        last_model: str = "unknown"
        async for chunk in chat_stream(messages=msgs, llm=llm_opts):
            if chunk.content:
                has_content = True
                yield chunk.content
            elif chunk.reasoning:
                reasoning_buffer.append(chunk.reasoning)
            if chunk.usage is not None:
                last_usage = chunk.usage
            if chunk.model:
                last_model = chunk.model
        metrics.record(
            app_name,
            last_model,
            time.monotonic() - start,
            tokens=int(getattr(last_usage, "total_tokens", 0) or 0),
            error=None,
        )
        if not has_content and reasoning_buffer:
            logger.warning("stream_chat: no content, yielding reasoning fallback")
            yield "".join(reasoning_buffer)

    async def embed(
        self,
        texts: list[str],
        timeout: float = 60.0,
        max_retries: int = 3,
        raise_on_error: bool = False,
    ) -> Optional[list[list[float]]]:
        await configure_ironman()
        from ironman import embed as _embed

        async def _do() -> list[list[float]]:
            return await _embed(text=texts)

        try:
            return await with_retry(_do, timeout, max_retries)
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