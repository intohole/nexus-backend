"""LLM 统一接入层: 网关路由、确定性prompt缓存、异步调用与重试."""
from __future__ import annotations

import os
import time
from typing import AsyncGenerator, Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.circuit_breaker import get_llm_circuit
from nexus.credits import CreditsInsufficientError, get_credits_service, report_llm_usage
from nexus.llm_metrics import llm_telemetry
from nexus.llm_utils import parse_llm_json, with_retry
from nexus.llm_helpers import (
    apply_output_discipline,
    convert_messages,
    extract_content,
    record_usage,
    resolve_namespace,
    stream_chunks,
)
from nexus.llm_budget import OutputMode, resolve_effective_budget
from nexus.llm_cache import PromptCache, get_prompt_cache
from nexus.llm_config import (
    configure_ironman,
    effective_retries,
    resolve_app_name,
)

logger = get_logger("nexus.llm")


async def _meter(kind: str, app_name: str, request_id: str, response: object = None,
                 start: float = 0.0, calls: int = 1) -> None:
    await report_llm_usage(app_name=app_name, kind=kind, request_id=request_id, response=response,
                           latency_s=(time.monotonic() - start) if start else 0.0, calls=calls)


async def _preflight(kind: str) -> None:
    """正式计费模式下调用前预检；CreditsInsufficientError(402) 放行，其余积分故障静默。"""
    try:
        await get_credits_service().gateway_preflight(kind)
    except CreditsInsufficientError:
        raise
    except Exception:
        pass


async def _charge(kind: str, app_name: str, request_id: str) -> None:
    """调用成功后网关自动计费（fail-open，绝不影响响应）。"""
    try:
        await get_credits_service().auto_charge_llm(
            kind=kind, app_name=app_name, request_id=request_id
        )
    except Exception:
        pass

DEFAULT_MAX_OUTPUT_TOKENS: int = int(os.environ.get("LLM_DEFAULT_MAX_OUTPUT_TOKENS", "2048"))


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

    async def _prepare_call(
        self,
        *,
        temperature: float,
        max_tokens: Optional[int],
        task_type: Optional[str],
        output_mode: Optional[OutputMode],
        json_mode: bool,
        namespace: Optional[str],
        model: Optional[str],
        enable_thinking: bool,
        thinking_budget: int,
    ) -> tuple[object, float, int, Optional[OutputMode]]:
        """公共前置：配置 ironman、预算解析并构造 LLMOptions。

        返回 (llm_opts, 生效温度, 生效 max_tokens, output_mode)。
        """
        await configure_ironman()
        budget_max, budget_temp, budget_mode = resolve_effective_budget(
            task_type, max_tokens, temperature, output_mode
        )
        temp = 0.7 if budget_temp is None else budget_temp
        eff_max: int = budget_max if budget_max is not None else DEFAULT_MAX_OUTPUT_TOKENS
        from ironman.types import LLMOptions

        opts = LLMOptions(
            temperature=temp,
            max_tokens=eff_max,
            model=model,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            extra=self._build_extra(json_mode, namespace, task_type),
        )
        return opts, temp, eff_max, budget_mode

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
        async with llm_telemetry(kind, app_name, request_id) as (metrics, start):
            async def _do_with_circuit() -> object:
                return await circuit.call(do)
            response: object = await with_retry(
                _do_with_circuit, timeout, effective_retries(max_retries)
            )
            result: str = extract_content(response, request_id)
            record_usage(metrics, app_name, response, time.monotonic() - start, None)
            await _meter(kind, app_name, request_id, response, start)
            logger.info(
                "LLM %s completed [req_id=%s, app=%s, latency=%.2fs]",
                kind, request_id, app_name, time.monotonic() - start,
            )
            return result

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
        model: Optional[str] = None,
        enable_thinking: bool = False,
        thinking_budget: int = 8192,
    ) -> str:
        opts, temp, eff_max, budget_mode = await self._prepare_call(
            temperature=temperature, max_tokens=max_tokens, task_type=task_type,
            output_mode=output_mode, json_mode=json_mode, namespace=namespace,
            model=model, enable_thinking=enable_thinking, thinking_budget=thinking_budget,
        )
        system, _ = apply_output_discipline(system, "", concise, json_mode, budget_mode)
        ironman_messages = convert_messages(messages, system)
        cache = get_prompt_cache() if temp <= 0.0 else None
        if cache is not None:
            key: str = PromptCache.make_messages_key(system, messages, temp, eff_max)
            hit: Optional[str] = cache.get(key)
            if hit is not None:
                return hit
        from ironman import chat as _chat
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        await _preflight("chat")

        async def _do() -> object:
            return await _chat(messages=ironman_messages, llm=opts)

        result = await self._execute(_do, timeout, max_retries, app_name, request_id, "chat")
        await _charge("chat", app_name, request_id)
        if cache is not None and result:
            cache.set(key, result)
        return result

    async def ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        """单轮提问 = 单条 user 消息的 chat。"""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        )

    async def ask_json(self, prompt: str, system: Optional[str] = None, **kwargs) -> dict[str, object]:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 1500)
        raw = await self.ask(prompt=prompt, system=system, json_mode=True, **kwargs)
        return parse_llm_json(raw)

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        **kwargs,
    ) -> dict[str, object]:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 1500)
        raw = await self.chat(messages=messages, system=system, json_mode=True, **kwargs)
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
        app_name: str = resolve_app_name()
        circuit = get_llm_circuit()
        await _preflight("extract")

        async def _do() -> object:
            return await _extract(
                prompt=prompt,
                schema=schema,
                llm=LLMOptions(),
            )

        try:
            async with llm_telemetry("extract", app_name, request_id) as (metrics, start):
                async def _do_with_circuit() -> object:
                    return await circuit.call(_do)
                result: object = await with_retry(
                    _do_with_circuit, timeout, effective_retries(max_retries)
                )
                metrics.record(app_name, "unknown", time.monotonic() - start, tokens=0, error=None)
                await _meter("extract", app_name, request_id, start=start)
                await _charge("extract", app_name, request_id)
                logger.info(
                    "LLM extract completed [req_id=%s, app=%s, latency=%.2fs]",
                    request_id, app_name, time.monotonic() - start,
                )
                return result
        except Exception:
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
        model: Optional[str] = None,
        enable_thinking: bool = False,
        thinking_budget: int = 8192,
    ) -> AsyncGenerator[str, None]:
        opts, _temp, _eff_max, budget_mode = await self._prepare_call(
            temperature=temperature, max_tokens=max_tokens, task_type=task_type,
            output_mode=output_mode, json_mode=False, namespace=namespace,
            model=model, enable_thinking=enable_thinking, thinking_budget=thinking_budget,
        )
        system, _ = apply_output_discipline(system, "", False, False, budget_mode)
        ironman_messages = convert_messages(messages, system)
        from ironman import chat_stream as _chat_stream
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        start: float = time.monotonic()
        await _preflight("chat_stream")
        produced: bool = False
        try:
            async for chunk in stream_chunks(_chat_stream, ironman_messages, opts):
                produced = True
                yield chunk
        finally:
            await _meter("chat_stream", app_name, request_id, start=start)
            if produced:
                await _charge("chat_stream", app_name, request_id)

    async def stream_ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> AsyncGenerator[str, None]:
        """单轮流式提问 = 单条 user 消息的 stream_chat。"""
        async for chunk in self.stream_chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        ):
            yield chunk

    async def embed(
        self,
        texts: list[str],
        timeout: float = 60.0,
        max_retries: int = 3,
        raise_on_error: bool = False,
    ) -> Optional[list[list[float]]]:
        await configure_ironman()
        from ironman import embed as _embed

        app_name: str = resolve_app_name()
        request_id: str = get_request_id() or "-"
        await _preflight("embed")

        async def _do() -> list[list[float]]:
            return await _embed(text=texts)

        try:
            result = await with_retry(_do, timeout, effective_retries(max_retries))
            await _meter("embed", app_name, request_id, calls=len(texts) or 1)
            await _charge("embed", app_name, request_id)
            return result
        except Exception as e:
            logger.error("Embed failed: %s: %s", type(e).__name__, e or "(无错误详情)")
            if raise_on_error:
                raise
            return None


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
