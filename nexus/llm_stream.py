"""LLM 流式调用族：stream_chat / stream_ask（计费与产出场强耦合，与核心族同源 _prepare_call）."""
from __future__ import annotations

import time
from typing import AsyncGenerator, Optional

from nexus.context import get_request_id
from nexus.credits import credits_gateway_handled
from nexus.llm_meter import charge as _charge, meter as _meter, preflight as _preflight
from nexus.llm_helpers import apply_output_discipline, convert_messages, stream_chunks
from nexus.llm_budget import OutputMode
from nexus.llm_config import resolve_app_name


class LLMStreamMixin:
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
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        feature: str = "",
    ) -> AsyncGenerator[str, None]:
        opts, _temp, _eff_max, budget_mode = await self._prepare_call(
            temperature=temperature, max_tokens=max_tokens, task_type=task_type,
            output_mode=output_mode, json_mode=False, namespace=namespace,
            model=model, enable_thinking=enable_thinking, thinking_budget=thinking_budget,
            reasoning_effort=reasoning_effort,
        )
        system, _ = apply_output_discipline(system, "", False, False, budget_mode)
        ironman_messages = convert_messages(messages, system)
        from ironman import chat_stream as _chat_stream
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        start: float = time.monotonic()
        await _preflight("chat_stream", model=str(getattr(opts, "model", "") or ""))
        produced: bool = False
        try:
            with credits_gateway_handled():
                async for chunk in stream_chunks(_chat_stream, ironman_messages, opts):
                    produced = True
                    yield chunk
        finally:
            await _meter("chat_stream", app_name, request_id, start=start, feature=feature)
            if produced:
                await _charge("chat_stream", app_name, request_id,
                              model=str(getattr(opts, "model", "") or ""))

    async def stream_ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> AsyncGenerator[str, None]:
        """单轮流式提问 = 单条 user 消息的 stream_chat。"""
        async for chunk in self.stream_chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        ):
            yield chunk
