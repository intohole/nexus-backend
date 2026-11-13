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
from nexus.llm_utils import usage_summary

__all__ = ["DEFAULT_MAX_OUTPUT_TOKENS", "LLMService", "get_llm_service"]


def _apply_json_primitive_fallback(kwargs: dict) -> None:
    """JSON 原语参数兜底单源（r94，#207 族）：无 task_type 时保持 r64 时代
    兜底（temperature 0.2/max_tokens 1500）；调用方声明 task_type 则温度/预算
    不表态，交档位解析（TASK_BUDGETS）——遗产默认值不得遮蔽档位列。
    """
    kwargs.setdefault("json_mode", True)
    if kwargs.get("task_type") is None:
        kwargs.setdefault("temperature", 0.2)
        kwargs.setdefault("max_tokens", 1500)


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
        _apply_json_primitive_fallback(kwargs)
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
        _apply_json_primitive_fallback(kwargs)
        parsed = await _chat_json_core(
            self, messages, system=system, root_type=root_type, list_key=list_key, **kwargs
        )
        if parsed is None:
            if raise_on_error:
                raise ValueError("chat_json: LLM output failed JSON shape gate after retries")
            return {"raw_response": ""}
        return parsed

    async def ask_json_with_usage(
        self,
        prompt: str,
        system: Optional[str] = None,
        *,
        schema: Optional[type] = None,
        repair_attempts: int = 1,
        root_type: type = dict,
        list_key: Optional[str] = None,
        raise_on_error: bool = True,
        **kwargs,
    ) -> tuple[object, dict[str, object]]:
        """JSON 面带账单通道（harness r109）：ask_json 的计费孪生，chat_with_usage 同族语义。

        返回 (数据, usage 聚合)。schema 路径走 verified_json_chat 修复环，每次
        wire 调用（含修复重试）逐笔记账；非 schema 路径走 ask_json 组合原语
        （形状 gate+有界重试，attempt>0 绕缓存自愈），全败返回 (None, usage) 或
        sentinel——失败调用不丢账单。json_mode 恒开；task_type 声明面与
        ask_json 一致（省略参交档位解析）。
        """
        _apply_json_primitive_fallback(kwargs)
        if schema is not None:
            from nexus.llm_verify import verified_json_chat

            call_kwargs = dict(kwargs)
            call_kwargs.pop("json_mode", None)
            bills: list[dict[str, object]] = []

            async def _billing_chat(**chat_kw: object) -> str:
                bill = await self.chat(_want_usage=True, **chat_kw)  # type: ignore[arg-type]
                if isinstance(bill, dict):
                    bills.append({k: v for k, v in bill.items() if k != "content"})
                    return str(bill.get("content") or "")
                return str(bill)

            data = await verified_json_chat(
                _billing_chat,
                [{"role": "user", "content": prompt}],
                schema=schema,
                repair_attempts=repair_attempts,
                raise_on_error=True,
                system=system,
                **call_kwargs,
            )
            return data, usage_summary(bills)
        result = await _ask_json_core(
            self, prompt, system=system, root_type=root_type, list_key=list_key,
            collect_usage=True, **kwargs,
        )
        data, usage = result
        if data is None:
            if raise_on_error:
                raise ValueError("ask_json: LLM output failed JSON shape gate after retries")
            return {"raw_response": ""}, usage
        return data, usage


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
