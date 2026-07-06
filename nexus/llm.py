from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.llm_metrics import get_llm_metrics
from nexus.circuit_breaker import get_llm_circuit
from nexus.llm_utils import parse_llm_json, with_retry, LLMTimeoutError, strip_code_fence

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
            _ironman_configured = True
            logger.info("Ironman configured from %s", path)
        else:
            logger.warning("Ironman config not found, using env vars")


def _effective_retries(max_retries: int) -> int:
    """P2: 网关模式下重试降为 1（网关已有 3 次 failover）。

    三层重试链路：goldenFish(3) × nexus-backend(max_retries) × gateway(3)
    - 非网关模式：3 × 3 × 3 = 27 次（过多）
    - 网关模式：3 × 1 × 3 = 9 次（合理）
    """
    try:
        from nexus.ironman import is_gateway_mode
        if is_gateway_mode():
            return 1
    except ImportError:
        pass
    return max_retries


def _resolve_app_name() -> str:
    """获取当前 App 名（用于 metrics 归因）。"""
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
    def _convert_messages(
        messages: list[dict[str, str]],
        system: Optional[str],
    ) -> list:
        """将 OpenAI 风格 messages 转为 ironman Message 列表。"""
        from ironman.types import Message, Role

        ironman_messages: list = []
        if system:
            ironman_messages.append(Message(role=Role.SYSTEM, content=system))
        for msg in messages:
            role_str = msg.get("role", "user")
            content = msg.get("content", "")
            if role_str == "user":
                ironman_messages.append(Message(role=Role.USER, content=content))
            elif role_str == "assistant":
                ironman_messages.append(Message(role=Role.ASSISTANT, content=content))
            elif role_str == "system":
                ironman_messages.append(Message(role=Role.SYSTEM, content=content))
        return ironman_messages

    async def chat(
        self,
        messages: list[dict[str, str]],
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> str:
        await configure_ironman()
        from ironman import chat as _chat
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        ironman_messages = self._convert_messages(messages, system)
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)

        async def _do() -> str:
            response = await _chat(messages=ironman_messages, llm=llm_opts)
            return response.content

        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            async def _do_with_circuit() -> str:
                return await circuit.call(_do)
            # P2: 网关模式下重试降为 1（网关已有 failover）
            result: str = await with_retry(
                _do_with_circuit, timeout, _effective_retries(max_retries)
            )
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=None)
            logger.info(
                "LLM chat completed [req_id=%s, app=%s, latency=%.2fs]",
                request_id, app_name, latency,
            )
            return result
        except Exception as e:
            latency = time.monotonic() - start
            error_type: str = type(e).__name__
            metrics.record(app_name, "unknown", latency, tokens=0, error=error_type)
            logger.error(
                "LLM chat failed [req_id=%s, app=%s, latency=%.2fs]: %s",
                request_id, app_name, latency, e,
            )
            raise

    async def ask(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> str:
        await configure_ironman()
        from ironman import ask as _ask
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()
        llm_opts = LLMOptions(temperature=temperature, max_tokens=max_tokens)

        async def _do() -> str:
            return await _ask(prompt=prompt, system=system, llm=llm_opts)

        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            async def _do_with_circuit() -> str:
                return await circuit.call(_do)
            # P2: 网关模式下重试降为 1（网关已有 failover）
            result: str = await with_retry(
                _do_with_circuit, timeout, _effective_retries(max_retries)
            )
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=None)
            logger.info(
                "LLM ask completed [req_id=%s, app=%s, latency=%.2fs]",
                request_id, app_name, latency,
            )
            return result
        except Exception as e:
            latency = time.monotonic() - start
            error_type: str = type(e).__name__
            metrics.record(app_name, "unknown", latency, tokens=0, error=error_type)
            logger.error(
                "LLM ask failed [req_id=%s, app=%s, latency=%.2fs]: %s",
                request_id, app_name, latency, e,
            )
            raise

    async def ask_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = 1500,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> dict[str, object]:
        raw = await self.ask(
            prompt=prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
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
    ) -> dict[str, object]:
        raw = await self.chat(
            messages=messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries,
        )
        return parse_llm_json(raw)

    async def extract(
        self,
        prompt: str,
        schema: Optional[type] = None,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> Optional[object]:
        await configure_ironman()
        from ironman import extract as _extract
        from ironman.types import LLMOptions

        request_id: str = get_request_id() or "-"
        app_name: str = _resolve_app_name()

        async def _do() -> object:
            return await _extract(
                prompt=prompt,
                schema=schema,
                llm=LLMOptions(),
                max_retries=0,
            )

        circuit = get_llm_circuit()
        metrics = get_llm_metrics()
        start: float = time.monotonic()
        try:
            async def _do_with_circuit() -> object:
                return await circuit.call(_do)
            # P2: 网关模式下重试降为 1
            result: object = await with_retry(
                _do_with_circuit, timeout, _effective_retries(max_retries)
            )
            latency: float = time.monotonic() - start
            metrics.record(app_name, "unknown", latency, tokens=0, error=None)
            logger.info(
                "LLM extract completed [req_id=%s, app=%s, latency=%.2fs]",
                request_id, app_name, latency,
            )
            return result
        except Exception as e:
            latency = time.monotonic() - start
            error_type: str = type(e).__name__
            metrics.record(app_name, "unknown", latency, tokens=0, error=error_type)
            # A4.2: 熔断器 OPEN 时向上抛出，让调用方感知网关故障（不吞掉）
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
            return None


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        _llm_service = LLMService()
    return _llm_service
