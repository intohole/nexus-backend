"""LLM 错误日志带响应体摘要（S155）：排障从「进程外复刻反推」变「日志一行看原文」。

生产判例（arcade r25）：judge 8 连撞 400 全灭，无 body 排障 1h。
ironman ProviderError 补 response 字段后，nexus 两处不可重试错误日志
（with_llm_retry / resilient_ask）必须透传 body 摘要（≤500 字符）。
"""
from __future__ import annotations

import httpx
import pytest

from nexus.llm_utils import error_body_snippet, with_llm_retry


def _provider_error(status: int, body: str) -> Exception:
    from ironman.errors import ProviderError

    request = httpx.Request("POST", "http://gw.test/v1/errprobe")
    response = httpx.Response(status, content=body.encode(), request=request)
    response.read()
    return ProviderError(provider="probe", message=f"Client error '{status}' for url", status_code=status, response=response)


def test_snippet_with_body() -> None:
    exc = _provider_error(400, '{"error":{"code":"InvalidSubscription"}}')
    assert "InvalidSubscription" in error_body_snippet(exc)


def test_snippet_without_response_returns_empty() -> None:
    from ironman.errors import ProviderError

    exc = ProviderError(provider="p", message="boom")
    assert error_body_snippet(exc) == ""


def test_snippet_truncated_to_500() -> None:
    exc = _provider_error(400, "x" * 3000)
    assert len(error_body_snippet(exc)) == 500


@pytest.mark.asyncio
async def test_with_llm_retry_logs_body_on_non_retryable(caplog) -> None:
    exc = _provider_error(400, '{"error":{"code":"1210","message":"API param invalid"}}')
    calls = {"n": 0}

    async def coro_fn() -> str:
        calls["n"] += 1
        raise exc

    with pytest.raises(Exception):
        with caplog.at_level("ERROR", logger="nexus.llm_utils"):
            await with_llm_retry(coro_fn, timeout=5.0, max_retries=3)
    assert calls["n"] == 1
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "1210" in joined
