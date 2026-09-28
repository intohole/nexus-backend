"""LLM JSON 客户端：结构化 JSON 输出的调用封装。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from nexus.llm import get_llm_service
from nexus.llm import parse_llm_json

logger = logging.getLogger("nexus.llm_client")

_BUDGET_RETRY_MIN = 8000
_BUDGET_RETRY_MAX = 16000


class LLMJsonClient:
    def __init__(
        self,
        *,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        temperature: float = 0.3,
        max_tokens: int = 3000,
        task_type: str = "default",
        timeout: float = 90.0,
        budget_retry: bool = True,
    ) -> None:
        self._svc = get_llm_service()
        self._last_error = ""
        self._model = model
        self._enable_thinking = enable_thinking
        self._thinking_budget = thinking_budget
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._task_type = task_type
        self._timeout = timeout
        self._budget_retry = budget_retry

    @property
    def last_error(self) -> str:
        return self._last_error

    @staticmethod
    def _is_parse_failed(result: object) -> bool:
        return isinstance(result, dict) and set(result.keys()) == {"raw_response"}

    def _thinking_kwargs(self, enable_thinking: Optional[bool], thinking_budget: Optional[int]) -> dict[str, object]:
        kwargs: dict[str, object] = {}
        eff_enable = self._enable_thinking if enable_thinking is None else enable_thinking
        eff_budget = self._thinking_budget if thinking_budget is None else thinking_budget
        if eff_enable is not None:
            kwargs["enable_thinking"] = eff_enable
        if eff_budget is not None:
            kwargs["thinking_budget"] = eff_budget
        return kwargs

    async def _ask_json_once(
        self,
        prompt: str,
        system: str,
        *,
        max_tokens: int,
        task_type: str,
        timeout: float,
        call: dict[str, object],
    ) -> Dict[str, Any]:
        raw = await self._svc.ask(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            json_mode=True,
            task_type=task_type,
            timeout=timeout,
            **call,
        )
        parsed = parse_llm_json(raw)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"LLM 返回非 dict 结构: {type(parsed).__name__}")

    async def ask_json(
        self,
        prompt: str,
        system: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        fallback: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        task_type: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        call: dict[str, object] = {
            "temperature": self._temperature if temperature is None else temperature,
            "model": model if model is not None else self._model,
        }
        call.update(self._thinking_kwargs(enable_thinking, thinking_budget))
        eff_task_type = self._task_type if task_type is None else task_type
        eff_timeout = self._timeout if timeout is None else timeout
        budget = self._max_tokens if max_tokens is None else max_tokens
        try:
            result = await self._ask_json_once(
                prompt, system,
                max_tokens=budget, task_type=eff_task_type, timeout=eff_timeout, call=call,
            )
            if self._is_parse_failed(result) and self._budget_retry:
                bigger = min(_BUDGET_RETRY_MAX, max(budget * 3, _BUDGET_RETRY_MIN))
                if bigger > budget:
                    logger.warning("JSON 解析失败，放大输出预算重试：%s -> %s", budget, bigger)
                    result = await self._ask_json_once(
                        prompt, system,
                        max_tokens=bigger, task_type=eff_task_type, timeout=eff_timeout, call=call,
                    )
            if not self._is_parse_failed(result):
                return result
        except Exception as exc:
            self._last_error = str(exc)
        if fallback is not None:
            return fallback
        raise ValueError(self._last_error or "LLM 返回无效 JSON")

    async def chat(
        self,
        messages: List[dict],
        system: str,
        *,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
    ) -> str:
        try:
            return await self._svc.chat(
                messages=messages,
                system=system,
                temperature=self._temperature if temperature is None else temperature,
                task_type=self._task_type,
                model=model if model is not None else self._model,
                enable_thinking=self._enable_thinking if enable_thinking is None else enable_thinking,
                thinking_budget=self._thinking_budget if thinking_budget is None else thinking_budget,
            )
        except Exception as exc:
            self._last_error = str(exc)
            raise

    async def ask_json_multimodal(
        self,
        prompt: str,
        system: str,
        images: List[str],
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        fallback: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        task_type: Optional[str] = None,
        timeout: Optional[float] = None,
        image_detail: str = "low",
    ) -> Dict[str, Any]:
        """多模态 JSON 调用：文本 prompt + 图片列表(URL/dataURL) 走统一 LLM 栈，供原生多模态模型使用。"""
        content: List[dict] = [{"type": "text", "text": prompt}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url, "detail": image_detail}})
        try:
            raw = await self._svc.chat(
                messages=[{"role": "user", "content": content}],
                system=system,
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens if max_tokens is None else max_tokens,
                json_mode=True,
                task_type=self._task_type if task_type is None else task_type,
                timeout=self._timeout if timeout is None else timeout,
                model=model if model is not None else self._model,
                **self._thinking_kwargs(enable_thinking, thinking_budget),
            )
            parsed = parse_llm_json(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            self._last_error = str(exc)
        if fallback is not None:
            return fallback
        raise ValueError(self._last_error or "LLM 多模态返回无效 JSON")
