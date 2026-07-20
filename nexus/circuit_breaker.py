"""熔断器 — 防止级联故障的标准模式。

状态机: CLOSED(正常) -> OPEN(熔断) -> HALF_OPEN(探测) -> CLOSED
源自 goldenFish 生产实践，上移为通用中间件能力。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional

from nexus.logging import get_logger

logger = get_logger("nexus.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5          # 连续失败达到该值后熔断
    recovery_timeout: float = 30.0      # OPEN 后经过该秒数进入 HALF_OPEN 探测
    half_open_max_calls: int = 3        # HALF_OPEN 状态允许的最大探测调用数
    success_threshold: int = 2          # HALF_OPEN 连续成功该次数后恢复 CLOSED
    excluded_exceptions: tuple = (TimeoutError,)  # 不计入失败的异常类型


@dataclass
class CircuitMetrics:
    total_calls: int = 0
    total_failures: int = 0
    total_successes: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    state_changes: List[Dict[str, object]] = field(default_factory=list)

    @property
    def failure_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_failures / self.total_calls


class CircuitBreakerOpenError(Exception):
    """熔断器处于 OPEN 状态时抛出。"""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        config: Optional[CircuitBreakerConfig] = None,
        on_state_change: Optional[Callable[[CircuitState, CircuitState], None]] = None,
    ):
        self._name = name
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._metrics = CircuitMetrics()
        self._half_open_calls = 0
        self._lock = asyncio.Lock()
        self._on_state_change = on_state_change

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def metrics(self) -> CircuitMetrics:
        return self._metrics

    async def call(self, func: Callable[..., Awaitable[object]], *args: object, **kwargs: object) -> object:
        async with self._lock:
            await self._update_state()
            if self._state == CircuitState.OPEN:
                raise CircuitBreakerOpenError(
                    f"Circuit '{self._name}' is OPEN. Retry after {self._config.recovery_timeout}s"
                )
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self._config.half_open_max_calls:
                    raise CircuitBreakerOpenError(f"Circuit '{self._name}' HALF_OPEN limit reached")
                self._half_open_calls += 1
        return await self._execute(func, *args, **kwargs)

    async def _execute(self, func: Callable[..., Awaitable[object]], *args: object, **kwargs: object) -> object:
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            await self._on_success()
            return result
        except self._config.excluded_exceptions:
            raise
        except Exception as e:
            await self._on_failure(e)
            raise

    async def _on_success(self) -> None:
        async with self._lock:
            self._metrics.total_calls += 1
            self._metrics.total_successes += 1
            self._metrics.consecutive_successes += 1
            self._metrics.consecutive_failures = 0
            self._metrics.last_success_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                if self._metrics.consecutive_successes >= self._config.success_threshold:
                    await self._transition_to(CircuitState.CLOSED)

    async def _on_failure(self, error: Exception) -> None:
        async with self._lock:
            self._metrics.total_calls += 1
            self._metrics.total_failures += 1
            self._metrics.consecutive_failures += 1
            self._metrics.consecutive_successes = 0
            self._metrics.last_failure_time = time.time()
            if self._state == CircuitState.HALF_OPEN:
                await self._transition_to(CircuitState.OPEN)
                return
            if self._metrics.consecutive_failures >= self._config.failure_threshold:
                await self._transition_to(CircuitState.OPEN)

    async def _update_state(self) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._metrics.last_failure_time
            if elapsed >= self._config.recovery_timeout:
                await self._transition_to(CircuitState.HALF_OPEN)
                self._half_open_calls = 0

    async def _transition_to(self, new_state: CircuitState) -> None:
        if self._state == new_state:
            return
        old_state = self._state
        self._state = new_state
        self._metrics.state_changes.append({"from": old_state.value, "to": new_state.value, "timestamp": time.time()})
        logger.warning("Circuit '%s' state: %s -> %s", self._name, old_state.value, new_state.value)
        if self._on_state_change:
            try:
                if asyncio.iscoroutinefunction(self._on_state_change):
                    await self._on_state_change(old_state, new_state)
                else:
                    self._on_state_change(old_state, new_state)
            except Exception as e:
                logger.error("Circuit state change callback error: %s", e)

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._metrics = CircuitMetrics()
        self._half_open_calls = 0
        logger.info("Circuit '%s' manually reset to CLOSED", self._name)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self._name,
            "state": self._state.value,
            "metrics": {
                "total_calls": self._metrics.total_calls,
                "total_failures": self._metrics.total_failures,
                "total_successes": self._metrics.total_successes,
                "failure_rate": round(self._metrics.failure_rate, 4),
                "consecutive_failures": self._metrics.consecutive_failures,
                "consecutive_successes": self._metrics.consecutive_successes,
            },
            "config": {
                "failure_threshold": self._config.failure_threshold,
                "recovery_timeout": self._config.recovery_timeout,
                "half_open_max_calls": self._config.half_open_max_calls,
                "success_threshold": self._config.success_threshold,
            },
        }


_circuit_breakers: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(name: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name=name, config=config)
    return _circuit_breakers[name]


def get_llm_circuit() -> CircuitBreaker:
    """LLM 网关专用熔断器单例（兼容 llm.py / ironman.py / fastapi_setup.py）。"""
    return get_circuit_breaker(
        "llm_gateway",
        config=CircuitBreakerConfig(
            failure_threshold=5,
            recovery_timeout=30.0,
            half_open_max_calls=3,
            success_threshold=2,
        ),
    )
