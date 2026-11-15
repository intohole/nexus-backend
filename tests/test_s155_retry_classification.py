"""LLM 4xx 重试分类守底（S155）：永久形态恒不可重试，瞬时形态恒可重试。

生产判据（pm gateway_usage_log 2026-11-16 取证，out/s155/segment1_census.md）：
- 402 Insufficient Balance 5,785 条：余额不足时间轴长永久，充值前重试纯放大
  ——原判据两表均无 402 落默认 return True=静默重试 3 次=缺陷。
- 499 client closed request 55 条：调用方已断连，重试结果无人收=浪费。
- 400 三类生产形态（InvalidSubscription/reasoning_content 构造/contentFilter）
  全为永久——维持不可重试（降档判据：无可靠瞬时签名不硬造）。
- 429/5xx 维持可重试（pm key_budget_exceeded 走 429=预算窗翻新可恢复语义）。
"""
from __future__ import annotations

import httpx
import pytest

from ironman.errors import ProviderError
from nexus.llm_utils import is_retryable_error, with_llm_retry


def _provider_error(status: int, body: str = "") -> Exception:
    request = httpx.Request("POST", "http://gw.test/v1/errprobe")
    response = httpx.Response(status, content=body.encode() or None, request=request)
    response.read()
    return ProviderError(provider="probe", message=f"Client error '{status}' for url", status_code=status, response=response)


def _httpx_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://gw.test/v1/errprobe")
    response = httpx.Response(status, request=request)
    response.read()
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")


PERMANENT = [400, 401, 402, 403, 404, 422, 499]
RETRYABLE = [429, 500, 502, 503, 504]


@pytest.mark.parametrize("status", PERMANENT)
def test_permanent_status_non_retryable(status: int) -> None:
    assert is_retryable_error(_provider_error(status)) is False


@pytest.mark.parametrize("status", RETRYABLE)
def test_transient_status_retryable(status: int) -> None:
    assert is_retryable_error(_provider_error(status)) is True


@pytest.mark.parametrize("status", PERMANENT)
def test_httpx_branch_permanent(status: int) -> None:
    assert is_retryable_error(_httpx_error(status)) is False


@pytest.mark.parametrize("status", RETRYABLE)
def test_httpx_branch_retryable(status: int) -> None:
    assert is_retryable_error(_httpx_error(status)) is True


def test_402_body_insufficient_balance() -> None:
    exc = _provider_error(402, '{"error":{"message":"Insufficient Balance"}}')
    assert is_retryable_error(exc) is False


@pytest.mark.asyncio
async def test_with_llm_retry_no_retry_on_402() -> None:
    calls = {"n": 0}

    async def coro_fn() -> str:
        calls["n"] += 1
        raise _provider_error(402, '{"error":{"message":"Insufficient Balance"}}')

    with pytest.raises(Exception):
        await with_llm_retry(coro_fn, timeout=5.0, max_retries=3)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_llm_retry_retries_on_429(monkeypatch) -> None:
    import asyncio as aio

    real_sleep = aio.sleep

    async def fast_sleep(delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("nexus.llm_utils.asyncio.sleep", fast_sleep)
    calls = {"n": 0}

    async def coro_fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _provider_error(429)
        return "ok"

    assert await with_llm_retry(coro_fn, timeout=5.0, max_retries=3) == "ok"
    assert calls["n"] == 3
