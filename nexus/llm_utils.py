from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("nexus.llm_utils")

T = TypeVar("T")


# P1: 不可重试的 HTTP 状态码（4xx 客户端错误，除 429 限流）
_NON_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("400", "401", "403", "404", "422")
# P1: 可重试的 HTTP 状态码（429 限流、5xx 服务端错误）
_RETRYABLE_STATUS_CODES: tuple[str, ...] = ("429", "500", "502", "503", "504")


class LLMTimeoutError(Exception):
    """LLM 调用超时异常。"""
    pass


def is_retryable_error(exc: Exception) -> bool:
    """P1: 判断异常是否值得重试。

    策略：
    - 熔断器 OPEN：不可重试（重试只会被熔断器再次拒绝）
    - 超时/连接错误：可重试（瞬时故障）
    - 4xx 客户端错误（除 429）：不可重试（请求本身有问题，重试无用）
    - 429 限流、5xx 服务端错误：可重试
    - 未知错误：保守重试（宁可多重试也不漏重试）
    """
    # A4.2: 熔断器 OPEN 状态：不可重试（用字符串匹配避免循环导入）
    exc_type_name: str = type(exc).__name__
    if exc_type_name == "CircuitBreakerOpenError":
        return False

    # 超时错误：可重试
    if isinstance(exc, (asyncio.TimeoutError, LLMTimeoutError)):
        return True
    # 连接错误：可重试
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    # httpx 连接/超时错误：可重试
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.PoolTimeout)):
            return True
    except ImportError:
        pass

    msg = str(exc).lower()
    # 不可重试：4xx 客户端错误（除 429）
    for code in _NON_RETRYABLE_STATUS_CODES:
        # 匹配 "status_code=401"、"returned 401"、" 401:" 等模式
        if f"status_code={code}" in msg or f"returned {code}" in msg or f" {code}:" in msg:
            return False
    # 可重试：429 限流、5xx 服务端错误
    for code in _RETRYABLE_STATUS_CODES:
        if code in msg:
            return True
    # 默认：未知错误重试（保守策略）
    return True


def _extract_retry_after(exc: Exception) -> float | None:
    """P3: 从异常消息中提取 retry_after（秒）。

    匹配 "retry_after": 5、retry_after=5、Retry-After: 5 等格式。
    返回 None 表示未找到。
    """
    msg = str(exc)
    patterns = [
        r'"retry_after"[:\s]+(\d+(?:\.\d+)?)',
        r"retry_after[=\s:]+(\d+(?:\.\d+)?)",
        r"Retry-After[=\s:]+(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, msg, re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                pass
    return None


def parse_llm_json(raw: str) -> dict[str, object]:
    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
        else:
            text = "\n".join(lines[1:])

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidate = m.group()
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        cleaned = re.sub(r",\s*}", "}", candidate)
        cleaned = re.sub(r",\s*]", "]", cleaned)
        cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        fixed = re.sub(r"\"(\w+)\"\s*:", r'"\1":', candidate)
        try:
            result = json.loads(fixed)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    if "```json" in text:
        parts = text.split("```json")
        if len(parts) > 1:
            json_block = parts[1].split("```")[0]
            try:
                result = json.loads(json_block.strip())
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass

    logger.warning("JSON parse failed after all attempts: %s", text[:300])
    return {"raw_response": text}


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    timeout: float = 60.0,
    max_retries: int = 3,
    non_retryable: tuple = (),
) -> T:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with asyncio.timeout(timeout):
                return await coro_fn()
        except asyncio.TimeoutError:
            last_error = LLMTimeoutError(
                f"Attempt {attempt + 1}/{max_retries} timeout after {timeout}s"
            )
            logger.warning("LLM timeout, attempt %d/%d", attempt + 1, max_retries)
            if attempt == max_retries - 1:
                raise last_error
            await asyncio.sleep(2.0 * (attempt + 1))
        except Exception as e:
            last_error = e
            if non_retryable and isinstance(e, non_retryable):
                logger.error("LLM non-retryable (business error): %s", e)
                raise
            if not is_retryable_error(e):
                logger.error("LLM non-retryable error (no retry): %s", e)
                raise
            logger.error("LLM error, attempt %d/%d: %s", attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                raise
            retry_after = _extract_retry_after(e)
            if retry_after is not None:
                wait_time = min(retry_after, 60.0)
                logger.info("LLM rate limited, waiting %ss (retry_after)", wait_time)
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(1.0 * (attempt + 1))
    raise last_error  # type: ignore[misc]


def strip_code_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1])
        return "\n".join(lines[1:])
    return text
