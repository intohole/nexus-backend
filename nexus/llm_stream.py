"""LLM 流式调用族：stream_chat / stream_ask（计费与产出场强耦合，与核心族同源 _prepare_call）."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

from nexus.context import get_request_id
from nexus.credits import credits_gateway_handled
from nexus.llm_budget import OutputMode
from nexus.llm_config import resolve_app_name
from nexus.llm_helpers import apply_output_discipline, convert_messages, stream_chunks
from nexus.llm_meter import charge as _charge
from nexus.llm_meter import meter as _meter
from nexus.llm_meter import preflight as _preflight


class LLMStreamMixin:
    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        feature: str = "",
        usage_sink: Optional[dict] = None,
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
        usage_read: dict = usage_sink if usage_sink is not None else {}
        try:
            with credits_gateway_handled():
                async for chunk in stream_chunks(_chat_stream, ironman_messages, opts, usage_sink=usage_read):
                    produced = True
                    yield chunk
        finally:
            await _meter("chat_stream", app_name, request_id, start=start, feature=feature)
            if produced and usage_read:
                # r272 刀28+M8（狩猎A P2存疑）：计费挪旁路任务——消费方提前 aclose 时
                # GeneratorExit 在飞，finally 内真实 await 网关 I/O=RuntimeError(ignored
                # GeneratorExit) 面。spawn_background 原语复用（异常 WARNING 留痕不上抛）。
                # 口径钉死：断流（消费方断连）usage_read 依赖 ironman r271 正门自然结束
                # 置位——断流=空读=不计费（漏记非错记，与 resilient_stream 中性退出声明
                # 对齐）；正常流计费恰好一次。
                from nexus.utils.background import spawn_background
                model_name = str(getattr(opts, "model", "") or "")
                spawn_background(_charge("chat_stream", app_name, request_id,
                                         model=model_name, usage=usage_read or None))

    async def stream_ask(self, prompt: str, system: Optional[str] = None, **kwargs) -> AsyncGenerator[str, None]:
        """单轮流式提问 = 单条 user 消息的 stream_chat。"""
        async for chunk in self.stream_chat(
            messages=[{"role": "user", "content": prompt}], system=system, **kwargs
        ):
            yield chunk

@dataclass
class StreamEvent:
    """r271 刀1b：流式事件面（delta=文本增量 / usage=末次用量回执）。

    「stream 缓存 token 业务侧回填」业务侧出口：usage 事件携带 input_tokens/
    output_tokens/cached_tokens/model（与 stream_chunks usage_sink 同源）。
    末次 usage 缺席（上游无 usage 帧）时 usage 事件不产出=诚实缺席。"""

    kind: str
    text: str = ""
    usage: Optional[dict] = None


class LLMStreamEventsMixin:
    async def stream_chat_events(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        output_mode: Optional[OutputMode] = None,
        namespace: Optional[str] = None,
        task_type: Optional[str] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        feature: str = "",
    ) -> AsyncGenerator[StreamEvent, None]:
        """stream_chat 的事件孪生（additive，存量 stream_chat 零改动）：文本走 delta
        事件，结束后按 usage_sink 读数产出 usage 事件（缓存 token 业务侧回填出口）。"""
        usage_read: dict = {}
        async for chunk in self.stream_chat(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            output_mode=output_mode,
            namespace=namespace,
            task_type=task_type,
            model=model,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            reasoning_effort=reasoning_effort,
            feature=feature,
            usage_sink=usage_read,
        ):
            yield StreamEvent(kind="delta", text=chunk)
        if usage_read:
            yield StreamEvent(kind="usage", usage=dict(usage_read))
