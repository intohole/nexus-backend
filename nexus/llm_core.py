"""LLM 统一接入层核心：单例、网关路由前置、确定性缓存与主链 chat."""
from __future__ import annotations

import os
import time
from typing import Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.circuit_breaker import get_llm_circuit
from nexus.credits import credits_gateway_handled
from nexus.llm_meter import charge as _charge, meter as _meter, preflight as _preflight
from nexus.llm_utils import with_llm_retry
from nexus.llm_helpers import (
    apply_output_discipline,
    convert_messages,
    extract_content,
    record_usage,
    resolve_namespace,
)
from nexus.llm_budget import OutputMode, canonicalize_task_type, resolve_effective_budget
from nexus.llm_cache import PromptCache, get_prompt_cache
from nexus.llm_config import configure_ironman, effective_retries, resolve_app_name
from nexus.llm_metrics import llm_telemetry

logger = get_logger("nexus.llm")

DEFAULT_MAX_OUTPUT_TOKENS: int = int(os.environ.get("LLM_DEFAULT_MAX_OUTPUT_TOKENS", "2048"))


class LLMCore:
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
        reasoning_effort: Optional[str] = None,
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
        if reasoning_effort:
            extra = dict(extra or {})
            extra["reasoning_effort"] = reasoning_effort
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
        reasoning_effort: Optional[str] = None,
        no_cache: bool = False,
    ) -> tuple[object, float, int, Optional[OutputMode]]:
        """公共前置：配置 ironman、预算解析并构造 LLMOptions。

        返回 (llm_opts, 生效温度, 生效 max_tokens, output_mode)。
        no_cache 全层禁用：跳过本地 PromptCache 读写并透传上游——同 prompt
        契约失败重试（nexus.llm_utils.ask_json）依赖此语义绕开坏缓存复读。
        """
        task_type = canonicalize_task_type(task_type)
        await configure_ironman()
        try:
            from ironman.tracing.request_scope import set_outbound_request_id

            from nexus.trace import ensure_request_id

            set_outbound_request_id(ensure_request_id())
        except ImportError:
            pass
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
            no_cache=no_cache,
            extra=self._build_extra(json_mode, namespace, task_type, reasoning_effort),
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
        feature: str = "",
    ) -> tuple[str, object]:
        circuit = get_llm_circuit()
        async with llm_telemetry(kind, app_name, request_id) as (metrics, start):
            async def _do_with_circuit() -> object:
                response: object = await circuit.call(do)
                extract_content(response, request_id)
                return response
            response: object = await with_llm_retry(
                _do_with_circuit, timeout, effective_retries(max_retries)
            )
            result: str = extract_content(response, request_id)
            record_usage(metrics, app_name, response, time.monotonic() - start, None)
            await _meter(kind, app_name, request_id, response, start, feature=feature)
            logger.info(
                "LLM %s completed [req_id=%s, app=%s, latency=%.2fs]",
                kind, request_id, app_name, time.monotonic() - start,
            )
            return result, response

    @staticmethod
    def _usage_payload(result: str, response: object) -> dict[str, object]:
        """从 ironman LLMResponse 提取调用方可见的用量账单（缓存命中时 response 为 None）。"""
        usage = getattr(response, "usage", None)
        return {
            "content": result,
            "model": getattr(response, "model", "") or "",
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
            "cost_usd": float(getattr(response, "cost_usd", 0.0) or 0.0),
            "latency_ms": int(float(getattr(response, "latency_ms", 0.0) or 0.0)),
            "provider": getattr(response, "provider", "") or "",
            "cached": bool(getattr(response, "cached", False)) or response is None,
        }

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
        reasoning_effort: Optional[str] = None,
        feature: str = "",
        no_cache: bool = False,
        _want_usage: bool = False,
    ) -> str | dict[str, object]:
        opts, temp, eff_max, budget_mode = await self._prepare_call(
            temperature=temperature, max_tokens=max_tokens, task_type=task_type,
            output_mode=output_mode, json_mode=json_mode, namespace=namespace,
            model=model, enable_thinking=enable_thinking, thinking_budget=thinking_budget,
            reasoning_effort=reasoning_effort, no_cache=no_cache,
        )
        system, _ = apply_output_discipline(system, "", concise, json_mode, budget_mode)
        ironman_messages = convert_messages(messages, system)
        cache = None if no_cache else (get_prompt_cache() if temp <= 0.0 else None)
        if cache is not None:
            key: str = PromptCache.make_messages_key(
                system, messages, temp, eff_max,
                model=str(getattr(opts, "model", "") or ""),
                namespace=resolve_namespace(namespace),
                enable_thinking=enable_thinking,
            )
            hit: Optional[str] = cache.get(key)
            if hit is not None:
                return self._usage_payload(hit, None) if _want_usage else hit
        from ironman import chat as _chat
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        await _preflight("chat", model=str(getattr(opts, "model", "") or ""))

        async def _do() -> object:
            with credits_gateway_handled():
                return await _chat(messages=ironman_messages, llm=opts)

        result, response = await self._execute(_do, timeout, max_retries, app_name, request_id, "chat", feature=feature)
        await _charge("chat", app_name, request_id, model=str(getattr(response, "model", "") or ""))
        if cache is not None and result:
            cache.set(key, result)
        return self._usage_payload(result, response) if _want_usage else result

    async def chat_with_usage(self, messages: list[dict[str, str]], **kwargs) -> dict[str, object]:
        """chat 的带账单版：返回 {content, model, total_tokens, cached_tokens, cost_usd, latency_ms, provider, cached}。

        计费/熔断/重试/缓存/json_mode 语义与 chat 完全一致；缓存命中时 usage 为 0 且 cached=True。
        """
        kwargs["_want_usage"] = True
        result = await self.chat(messages=messages, **kwargs)
        assert isinstance(result, dict)
        return result

    async def ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        """单轮提问 = 单条 user 消息的 chat。"""
        return await self.chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        )
