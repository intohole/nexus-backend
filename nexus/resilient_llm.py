"""LLM 韧性调用 — 成本守卫 + 熔断器 + 限流 + 重试 + 降级的标准组合。

所有调用 LLM 的应用统一入口，避免每个项目重复实现容错逻辑。
使用方式:
    from nexus import resilient_ask
    reply = await resilient_ask(prompt, temperature=0.5, fallback="默认回复", operation="intent")
"""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from typing import Awaitable, Callable, Optional

from nexus.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    get_circuit_breaker,
)
from nexus.cost_guard import CostBudgetExceededError, get_cost_guard
from nexus.llm import get_llm_service
from nexus.llm_utils import parse_llm_json
from nexus.logging import get_logger

logger = get_logger("nexus.resilient_llm")

_CB_CONFIG = CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=60.0,
    half_open_max_calls=2,
    success_threshold=2,
)


async def resilient_ask(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: Optional[int] = 2000,
    alias: str = "default",
    timeout: float = 30.0,
    fallback: Optional[str] = None,
    operation: str = "",
    estimated_tokens: int = 500,
    retry_count: int = 2,
    retry_delay: float = 1.0,
    use_rate_limit: bool = False,
    on_fallback: Optional[Callable[[str], None]] = None,
) -> str:
    """带成本守卫/熔断/重试/降级的 LLM 调用。

    alias 用于熔断器隔离与成本归集（同一 alias 共享熔断状态）。
    fallback 非 None 时，任何失败都返回 fallback 而不是抛异常。
    use_rate_limit=True 时额外启用 LLMRateLimiter 全局限流（高并发场景）。
    system 为可选的系统提示词，max_tokens 限制输出长度，透传底层 LLMService.ask。
    """
    cb = get_circuit_breaker(f"llm_{alias}", config=_CB_CONFIG)
    cost_guard = get_cost_guard()

    budget_check = await cost_guard.check_budget(estimated_tokens=estimated_tokens, model=alias)
    if not budget_check["allowed"]:
        logger.warning("CostGuard blocked LLM call: %s", budget_check["reason"])
        if fallback is not None:
            if on_fallback:
                on_fallback("budget_exceeded")
            return fallback
        raise CostBudgetExceededError(str(budget_check["reason"]))

    last_error: Optional[Exception] = None
    for attempt in range(retry_count + 1):
        try:
            if use_rate_limit:
                from nexus.llm_rate_limiter import get_llm_rate_limiter
                async with get_llm_rate_limiter().limited(caller=alias):
                    result = await cb.call(_call_llm, prompt=prompt, system=system, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            else:
                result = await cb.call(_call_llm, prompt=prompt, system=system, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            await cost_guard.record_usage(
                prompt_tokens=estimated_tokens // 2,
                completion_tokens=estimated_tokens // 2,
                model=alias,
                operation=operation or "llm_ask",
            )
            return result
        except CircuitBreakerOpenError:
            logger.warning("Circuit breaker OPEN for %s, using fallback", alias)
            if fallback is not None:
                if on_fallback:
                    on_fallback("circuit_open")
                return fallback
            raise
        except Exception as e:
            last_error = e
            logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, retry_count + 1, e)
            if attempt < retry_count:
                delay = retry_delay * (2 ** attempt)
                await asyncio.sleep(random.uniform(delay * 0.5, delay))

    logger.error("LLM call exhausted all retries: %s", last_error)
    if fallback is not None:
        if on_fallback:
            on_fallback("max_retries")
        return fallback
    raise last_error or RuntimeError("LLM call failed")


async def _call_llm(prompt: str, system: str, temperature: float, max_tokens: Optional[int], timeout: float) -> str:
    svc = get_llm_service()
    return await asyncio.wait_for(
        svc.ask(prompt, system=system, temperature=temperature, max_tokens=max_tokens, timeout=timeout),
        timeout=timeout + 5,
    )


async def resilient_extract(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.4,
    max_tokens: Optional[int] = 2000,
    alias: str = "default",
    timeout: float = 60.0,
    operation: str = "",
    estimated_tokens: int = 500,
    retry_count: int = 2,
    retry_delay: float = 1.0,
    use_rate_limit: bool = False,
) -> dict[str, object]:
    """带成本守卫/熔断/重试/降级的 LLM JSON 抽取。

    失败或解析失败返回 {}（供调用方规则化降级），不抛异常。
    """
    raw = await resilient_ask(
        prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        alias=alias,
        timeout=timeout,
        fallback="",
        operation=operation,
        estimated_tokens=estimated_tokens,
        retry_count=retry_count,
        retry_delay=retry_delay,
        use_rate_limit=use_rate_limit,
    )
    if not raw:
        return {}
    parsed = parse_llm_json(raw)
    if "raw_response" in parsed:
        logger.warning("resilient_extract parse failed for %s, fallback to empty", alias)
        return {}
    return parsed


async def resilient_stream(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.4,
    max_tokens: int = 2000,
    alias: str = "default",
) -> AsyncGenerator[str, None]:
    """韧性流式 LLM 调用。异常 re-raise，供调用方感知中断并发送错误事件。"""
    svc = get_llm_service()
    try:
        async for token in svc.stream_ask(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield token
    except Exception as e:
        logger.warning("resilient_stream %s failed: %s", alias, e)
        raise
