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
from typing import Callable, Optional

from nexus.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    get_circuit_breaker,
)
from nexus.cost_guard import CostBudgetExceededError, get_cost_guard
from nexus.llm import get_llm_service
from nexus.llm_utils import error_body_snippet, is_retryable_error
from nexus.llm_json import parse_llm_json
from nexus.logging import get_logger

logger = get_logger("nexus.resilient_llm")

_CB_CONFIG = CircuitBreakerConfig(
    failure_threshold=5,
    recovery_timeout=60.0,
    half_open_max_calls=2,
    success_threshold=2,
)


def _alias_circuit_name(alias: str) -> str:
    """alias 熔断闸聚合命名：llm:alias/{alias} 入 /llm-circuit 观测面（r243 差距 10）。

    前身 llm_{alias} 平行命名空间绕开 llm: 前缀过滤，聚合面不可见；
    迁移后与 llm:{provider} 通道闸同族（alias 粒度 vs provider 粒度，键结构区分）。
    """
    return f"llm:alias/{alias}"


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
    on_fallback: Optional[Callable[[str], None]] = None,
    namespace: Optional[str] = None,
    task_type: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
    thinking_budget: Optional[int] = None,
) -> str:
    """带成本守卫/熔断/重试/降级的 LLM 调用。

    alias 用于熔断器隔离与成本归集（同一 alias 共享熔断状态）。
    fallback 非 None 时，任何失败都返回 fallback 而不是抛异常。
    system 为可选的系统提示词，max_tokens 限制输出长度，透传底层 LLMService.ask。
    enable_thinking/thinking_budget 显式传入时直达 llm_core.chat（覆盖档位默认，
    判断型对话/推演链开思考的透传面——显式传参恒优先语义与 chat 层一致）。
    重试唯一样板：retry_count+1 次调用，内层 LLMService 已禁用重试（max_retries=0），不再叠加放大。
    """
    cb = get_circuit_breaker(_alias_circuit_name(alias), config=_CB_CONFIG)
    cost_guard = get_cost_guard()

    budget_check = await cost_guard.check_budget(estimated_tokens=estimated_tokens)
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
            async def _guarded() -> str:
                return await _call_llm(
                    prompt, system=system, temperature=temperature,
                    max_tokens=max_tokens, timeout=timeout,
                    namespace=namespace, task_type=task_type,
                    enable_thinking=enable_thinking, thinking_budget=thinking_budget,
                )

            result = await cb.call(_guarded, timeout=timeout)
            # ask 面无 usage 回传（chat 文本链接口不含 bill），记账对齐 call_with_guard
            # 降级链同款口径：整笔记 completion（保守侧），model 留空走 fallback 定价
            # 不刷 unknown warning（alias 是调用方分组名非定价键，//2 硬拆随负载形状失真）
            await cost_guard.record_usage(
                prompt_tokens=0,
                completion_tokens=estimated_tokens,
                model="",
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
            if not is_retryable_error(e):
                logger.error("LLM non-retryable error, giving up: %s | body=%s", e, error_body_snippet(e))
                break
            logger.warning("LLM call failed (attempt %s/%s): %s", attempt + 1, retry_count + 1, e)
            if attempt < retry_count:
                delay = retry_delay * (2 ** attempt)
                await asyncio.sleep(random.uniform(delay * 0.5, delay))

    logger.error("LLM call exhausted all retries: %s | body=%s", last_error, error_body_snippet(last_error))
    if fallback is not None:
        if on_fallback:
            on_fallback("max_retries")
        return fallback
    raise last_error or RuntimeError("LLM call failed")


async def _call_llm(prompt: str, system: str, temperature: float, max_tokens: Optional[int], timeout: float, namespace: Optional[str] = None, task_type: Optional[str] = None, enable_thinking: Optional[bool] = None, thinking_budget: Optional[int] = None) -> str:
    svc = get_llm_service()
    return await svc.ask(prompt, system=system, temperature=temperature, max_tokens=max_tokens, timeout=timeout, max_retries=0, namespace=namespace, task_type=task_type, enable_thinking=enable_thinking, thinking_budget=thinking_budget)


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
    namespace: Optional[str] = None,
    task_type: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
    thinking_budget: Optional[int] = None,
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
        namespace=namespace,
        task_type=task_type,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget,
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
    namespace: Optional[str] = None,
    task_type: Optional[str] = None,
    enable_thinking: Optional[bool] = None,
    thinking_budget: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """韧性流式 LLM 调用。异常 re-raise，供调用方感知中断并发送错误事件。

    守卫三件套与 resilient_ask 同构（r243 差距 9 收口）：预算闸流前拦截、
    alias 熔断闸三段式（acquire_call 前置+record_outcome 记账）、流完保守
    估算记账（流式无 usage 回传，ask 面同口径）。消费方提前断开（aclose/
    取消）为中性退出：不计熔断成败、不记账。流中失败不记账（ask 判例对齐）。
    """
    cb = get_circuit_breaker(_alias_circuit_name(alias), config=_CB_CONFIG)
    cost_guard = get_cost_guard()
    budget_check = await cost_guard.check_budget(estimated_tokens=max_tokens)
    if not budget_check["allowed"]:
        logger.warning("CostGuard blocked LLM stream: %s", budget_check["reason"])
        raise CostBudgetExceededError(str(budget_check["reason"]))
    await cb.acquire_call()

    error: Optional[BaseException] = None
    aborted = False
    try:
        async for token in get_llm_service().stream_ask(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            namespace=namespace,
            task_type=task_type,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
        ):
            yield token
        await cost_guard.record_usage(
            prompt_tokens=0,
            completion_tokens=max_tokens,
            model="",
            operation="llm_stream",
        )
    except (asyncio.CancelledError, GeneratorExit):
        aborted = True
        raise
    except Exception as e:
        # r270 评审修正②：ironman 刀8① 起 abort 打穿本层——AbortedError=预算帽/限流
        # 中止的一等退出（非 provider 故障），中性退出不计熔断失败（否则正常流量被
        # 误开熔断）；CancelledError 分支同语义先例。
        from ironman.middleware.pipeline import AbortedError

        if isinstance(e, AbortedError):
            aborted = True
            raise
        error = e
        logger.warning("resilient_stream %s failed: %s", alias, e)
        raise
    finally:
        if aborted:
            # r272 刀21+M7：中性退出显式归还探测配额（取消/断连不再烧穿 HALF_OPEN
            # 配额；docstring「中性退出不计熔断成败」自此处起真正兑现到配额面）
            await cb.release_neutral()
        else:
            await cb.record_outcome(error)
