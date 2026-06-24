from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("nexus.llm_utils")

T = TypeVar("T")


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


class LLMTimeoutError(Exception):
    pass


async def with_retry(
    coro_fn: Callable[[], Awaitable[T]],
    timeout: float = 60.0,
    max_retries: int = 3,
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
            logger.error("LLM error, attempt %d/%d: %s", attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                raise
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
