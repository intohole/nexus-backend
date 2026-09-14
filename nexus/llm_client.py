from __future__ import annotations

from typing import Any, Dict, List, Optional

from nexus.llm import get_llm_service
from nexus.llm import parse_llm_json


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

    @property
    def last_error(self) -> str:
        return self._last_error

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
        try:
            raw = await self._svc.ask(
                prompt=prompt,
                system=system,
                temperature=self._temperature if temperature is None else temperature,
                max_tokens=self._max_tokens if max_tokens is None else max_tokens,
                json_mode=True,
                task_type=self._task_type if task_type is None else task_type,
                timeout=self._timeout if timeout is None else timeout,
                model=model if model is not None else self._model,
                enable_thinking=self._enable_thinking if enable_thinking is None else enable_thinking,
                thinking_budget=self._thinking_budget if thinking_budget is None else thinking_budget,
            )
            parsed = parse_llm_json(raw)
            if isinstance(parsed, dict):
                return parsed
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
