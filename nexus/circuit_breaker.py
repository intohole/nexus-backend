"""LLM 熔断器（A4.2）。

当 prompt-manager 网关持续失败时，业务 App 快速失败而非重试 180s。
单例熔断器，所有 LLM 调用共用（因为都走同一个网关）。

状态机：
- CLOSED：正常调用，连续 5 次失败 → OPEN
- OPEN：快速失败（抛 CircuitBreakerOpenError），30s 后 → HALF_OPEN
- HALF_OPEN：允许试探调用，连续 2 次成功 → CLOSED；任意失败 → OPEN
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitConfig:
    """熔断器配置。"""

    failure_threshold: int = 5        # 连续 N 次失败 → OPEN
    recovery_timeout: float = 30.0    # OPEN 持续 N 秒后 → HALF_OPEN
    success_threshold: int = 2        # HALF_OPEN 连续 N 次成功 → CLOSED


class CircuitBreakerOpenError(Exception):
    """熔断器 OPEN 状态时抛出，应被视为不可重试错误。"""

    pass


class CircuitBreaker:
    """异步熔断器（参考 goldenFish 实现，简化为 nexus 层共用）。"""

    def __init__(
        self,
        name: str,
        config: Optional[CircuitConfig] = None,
    ) -> None:
        self._name: str = name
        self._config: CircuitConfig = config or CircuitConfig()
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_failure_time: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def consecutive_successes(self) -> int:
        return self._consecutive_successes

    async def call(
        self,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """通过熔断器调用异步函数。

        - CLOSED/HALF_OPEN：允许调用，根据结果更新状态
        - OPEN：直接抛 CircuitBreakerOpenError，不调用 func
        """
        async with self._lock:
            await self._maybe_recover()
            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError(
                    f"Circuit '{self._name}' OPEN "
                    f"(failures={self._consecutive_failures}), "
                    f"retry after {self._config.recovery_timeout}s"
                )

        try:
            result: Any = await func(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as exc:
            await self._on_failure()
            raise

    async def _maybe_recover(self) -> None:
        """OPEN 状态超时后转为 HALF_OPEN（在锁内调用）。"""
        if self._state == CircuitState.OPEN:
            elapsed: float = time.monotonic() - self._last_failure_time
            if elapsed >= self._config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                logger.warning(
                    "Circuit '%s' OPEN -> HALF_OPEN (after %.1fs)",
                    self._name,
                    elapsed,
                )

    async def _on_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._consecutive_successes += 1
            if (
                self._state == CircuitState.HALF_OPEN
                and self._consecutive_successes >= self._config.success_threshold
            ):
                self._state = CircuitState.CLOSED
                logger.info(
                    "Circuit '%s' HALF_OPEN -> CLOSED (successes=%d)",
                    self._name,
                    self._consecutive_successes,
                )

    async def _on_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_time = time.monotonic()
            if (
                self._consecutive_failures >= self._config.failure_threshold
                and self._state != CircuitState.OPEN
            ):
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit '%s' -> OPEN (consecutive_failures=%d)",
                    self._name,
                    self._consecutive_failures,
                )

    def to_dict(self) -> dict[str, Any]:
        """返回熔断器状态快照（供 /api/_internal/llm-circuit 端点）。"""
        return {
            "name": self._name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "config": {
                "failure_threshold": self._config.failure_threshold,
                "recovery_timeout": self._config.recovery_timeout,
                "success_threshold": self._config.success_threshold,
            },
        }


# 单例熔断器：所有 LLM 调用共用（针对 prompt-manager 网关）
_llm_circuit: Optional[CircuitBreaker] = None


def get_llm_circuit() -> CircuitBreaker:
    """获取 LLM 熔断器单例。

    同步函数：单例创建无需异步锁（Python GIL 保证赋值原子性，
    CircuitBreaker 内部的 asyncio.Lock 负责状态管理）。
    """
    global _llm_circuit
    if _llm_circuit is None:
        _llm_circuit = CircuitBreaker("llm_gateway")
    return _llm_circuit
