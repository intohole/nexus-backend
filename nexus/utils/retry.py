"""通用异步重试原语：指数退避 + 抖动 + 可定制重试判定。"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

import httpx

logger = logging.getLogger("nexus.retry")

T = TypeVar("T")

RETRYABLE_HTTP_STATUS: set[int] = {408, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    httpx.ConnectError,
    asyncio.TimeoutError,
)


def is_retryable_error(error: Exception) -> bool:
    if isinstance(error, RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_HTTP_STATUS
    return False


def backoff_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    delay = min(base_delay * (2**attempt), max_delay)
    return delay + random.uniform(0, delay * 0.5)


class RetryExhausted(Exception):
    def __init__(self, last_error: Exception, attempts: int) -> None:
        self.last_error: Exception = last_error
        self.attempts: int = attempts
        super().__init__(f"Retry exhausted after {attempts} attempts: {last_error}")


async def with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: object,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_on: Optional[Callable[[Exception], bool]] = None,
    raise_on_exhaust: bool = False,
    **kwargs: object,
) -> Optional[T]:
    """对 fn 的单次异步调用做指数退避重试。

    retry_on 缺省按 RETRYABLE_EXCEPTIONS/RETRYABLE_HTTP_STATUS 判定；
    判定不可重试时 raise_on_exhaust=True 原样上抛、False 返回 None；
    重试预算耗尽时 raise_on_exhaust=True 抛 RetryExhausted（链上原异常）。
    """
    last_error: Optional[Exception] = None
    name = getattr(fn, "__name__", repr(fn))
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_error = exc
            should_retry = retry_on(exc) if retry_on else is_retryable_error(exc)
            if not should_retry:
                logger.warning("Non-retryable error from %s: %s", name, exc)
                if raise_on_exhaust:
                    raise
                return None
            if attempt >= max_retries:
                break
            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "Retryable error (attempt %d/%d) from %s, retrying in %.1fs: %s",
                attempt + 1,
                max_retries,
                name,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    logger.error("Retry exhausted after %d attempts: %s", max_retries + 1, last_error)
    if raise_on_exhaust and last_error is not None:
        raise RetryExhausted(last_error, max_retries + 1) from last_error
    return None
