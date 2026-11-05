"""LLM 统一接入层（拆分薄壳层）：LLMService = 核心调用 + 探针/旁路 + 流式 MixIn 组合.

核心调用族在 llm_core（单例与 chat 主链），探针/extract/embed 在 llm_probes，
流式族在 llm_stream；ask_json/chat_json 薄包装与单例工厂留本模块。
消费面旧名（LLMService/get_llm_service/DEFAULT_MAX_OUTPUT_TOKENS）不变。
"""
from __future__ import annotations

from typing import Optional

from nexus.llm_core import DEFAULT_MAX_OUTPUT_TOKENS, LLMCore
from nexus.llm_probes import LLMProbesMixin
from nexus.llm_stream import LLMStreamMixin
from nexus.llm_utils import ask_json as _ask_json_core
from nexus.llm_utils import chat_json as _chat_json_core

__all__ = ["DEFAULT_MAX_OUTPUT_TOKENS", "LLMService", "get_llm_service"]


class LLMService(LLMCore, LLMProbesMixin, LLMStreamMixin):
    async def ask_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        raise_on_error: bool = True,
        root_type: type = dict,
        list_key: Optional[str] = None,
        **kwargs,
    ) -> object:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 1500)
        kwargs.setdefault("json_mode", True)
        parsed = await _ask_json_core(
            self, prompt, system=system, root_type=root_type, list_key=list_key, **kwargs
        )
        if parsed is None:
            if raise_on_error:
                raise ValueError("ask_json: LLM output failed JSON shape gate after retries")
            return {"raw_response": ""}
        return parsed

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        raise_on_error: bool = True,
        root_type: type = dict,
        list_key: Optional[str] = None,
        **kwargs,
    ) -> dict[str, object]:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 1500)
        kwargs.setdefault("json_mode", True)
        parsed = await _chat_json_core(
            self, messages, system=system, root_type=root_type, list_key=list_key, **kwargs
        )
        if parsed is None:
            if raise_on_error:
                raise ValueError("chat_json: LLM output failed JSON shape gate after retries")
            return {"raw_response": ""}
        return parsed


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
