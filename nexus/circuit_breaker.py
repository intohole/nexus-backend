"""熔断器 — 防止级联故障的标准模式。

状态机: CLOSED(正常) -> OPEN(熔断) -> HALF_OPEN(探测) -> CLOSED
源自 goldenFish 生产实践，上移为通用中间件能力。

r227 LLM 面换核：通道级熔断（llm:{provider} 键）替代全局 llm_gateway 单闸——
一家通道失败不再锁死全舰队 ironman 链（litellm per-deployment cooldown 同构）。
通用 get_circuit_breaker(name) 面零变化（goldenFish/piying 自管名空间不受扰）。
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
    excluded_exceptions: tuple = ()     # 不计入失败的异常类型（超时不算失败是盲区，默认不豁免）


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
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def metrics(self) -> CircuitMetrics:
        return self._metrics

    async def acquire_call(self) -> None:
        """前置段：OPEN 拒绝 / HALF_OPEN 占用探测配额。与记账段分离供通道级三段式。"""
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

    async def record_outcome(self, error: Optional[BaseException]) -> None:
        """记账段：None=成功（half-open 连胜推进恢复），异常=失败。"""
        if error is None:
            await self._on_success()
        else:
            await self._on_failure(error)

    async def release_neutral(self) -> None:
        """r272 刀21+M7（狩猎D P1）：中性归还——取消/断连的探测名额只烧不还=
        HALF_OPEN 配额烧穿后 OPEN↔HALF_OPEN 弹跳（provider 愈后仍不开闸）。

        中性语义三不（复核席钉死）：不记失败、不进连胜（防取消被当恢复信号假
        开闸）、仅归还探测配额。仅 HALF_OPEN 态有配额可还；OPEN/CLOSED 幂等。"""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN and self._half_open_calls > 0:
                self._half_open_calls -= 1

    async def call(
        self,
        func: Callable[..., Awaitable[object]],
        *args: object,
        timeout: Optional[float] = None,
        **kwargs: object,
    ) -> object:
        """执行 func 并纳入熔断记账。

        timeout 内化到被看守函数：超时由熔断器自己施加（asyncio.timeout），
        到点必然计一次失败——外部 wait_for/asyncio.timeout 包装的"取消型超时"
        仍走 CancelledError 分支兜底计数，但外部主动取消（如客户端断连）无法
        与之区分，属保守误计（宁可多熔不可漏熔）。
        """
        await self.acquire_call()
        return await self._execute(func, *args, timeout=timeout, **kwargs)

    async def _execute(
        self,
        func: Callable[..., Awaitable[object]],
        *args: object,
        timeout: Optional[float] = None,
        **kwargs: object,
    ) -> object:
        try:
            if timeout:
                async with asyncio.timeout(timeout):
                    result = await func(*args, **kwargs)
            else:
                result = await func(*args, **kwargs)
            await self._on_success()
            return result
        except asyncio.TimeoutError:
            # 内化超时到点：真实失败，必然计数（asyncio.timeout 已把取消转为本异常）
            await self._on_failure(TimeoutError(f"Circuit '{self._name}' call timed out after {timeout}s"))
            raise
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                await self._on_failure(TimeoutError(f"Circuit '{self._name}' call cancelled (likely timeout)"))
            else:
                # r272 刀21+M7：未计数取消（GeneratorExit/裸取消）中性归还探测配额
                # （旧形态此处配额只烧不还；保守计数面维持已裁决口径不动）
                await self.release_neutral()
            raise
        except self._config.excluded_exceptions:
            # r272 刀21+M7：豁免异常不记成败，配额同步归还
            await self.release_neutral()
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
_LLM_CIRCUIT_PREFIX = "llm:"
_LLM_CONFIG = CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=30.0,
    half_open_max_calls=3,
    success_threshold=2,
)


def get_circuit_breaker(name: str, config: Optional[CircuitBreakerConfig] = None) -> CircuitBreaker:
    if name not in _circuit_breakers:
        _circuit_breakers[name] = CircuitBreaker(name=name, config=config)
    return _circuit_breakers[name]


def _ironman_default_provider() -> Optional[str]:
    """读 ironman 当前默认通道名（execute_with_pipeline actual_provider 同源语义）。"""
    try:
        from ironman.config import ConfigFactory

        return ConfigFactory.get().llm_default.provider or None
    except Exception:
        return None


def llm_provider_key(llm_opts: object = None) -> str:
    """LLM 熔断通道键解析单源：显式 provider > ironman 默认通道 > "default"。

    键粒度=provider 通道（故障域），非 model——同一通道多模型共享受限/宕机面，
    与 ironman fallback_chain、pm 网关 trip_breaker 同粒度（litellm per-deployment 同构）。
    """
    explicit = str(getattr(llm_opts, "provider", "") or "") if llm_opts is not None else ""
    if explicit:
        return explicit
    return _ironman_default_provider() or "default"


def get_llm_circuit(provider: str) -> CircuitBreaker:
    """LLM 通道级熔断器（r227 换核：全局 llm_gateway 单闸退役——一家通道失败
    不再锁死其他通道）。registry 键带 llm: 前缀，与通用 get_circuit_breaker
    名空间隔离；param 必填，调用点必须先经 llm_provider_key 解析。"""
    return get_circuit_breaker(f"{_LLM_CIRCUIT_PREFIX}{provider}", config=_LLM_CONFIG)


async def record_llm_outcome(entry_key: str, error: Optional[BaseException], response: object = None) -> str:
    """按实际通道记账（r227 失败归因单源）：异常带 .provider 归因异常通道，
    成功响应带 .provider 归因响应通道，都缺则回落入口键（fallback 换通道后
    失败落在最后失败通道头上，不再连坐入口/其他通道）。返回实际记账键。"""
    actual = ""
    if error is not None:
        actual = str(getattr(error, "provider", "") or "")
    elif response is not None:
        actual = str(getattr(response, "provider", "") or "")
    key = actual or entry_key
    await get_llm_circuit(key).record_outcome(error)
    return key


def all_llm_circuits() -> Dict[str, Dict[str, object]]:
    """观测聚合：全部 LLM 通道闸快照（/llm-circuit 端点消费）。"""
    return {
        name[len(_LLM_CIRCUIT_PREFIX):]: breaker.to_dict()
        for name, breaker in _circuit_breakers.items()
        if name.startswith(_LLM_CIRCUIT_PREFIX)
    }
