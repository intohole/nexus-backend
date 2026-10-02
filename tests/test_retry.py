"""nexus.utils.retry 通用重试原语回归。"""
import asyncio

import httpx
import pytest

from nexus.utils import RetryExhausted, backoff_delay, is_retryable_error, with_retry


def _request_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


class TestIsRetryableError:
    def test_connect_error_retryable(self):
        assert is_retryable_error(_request_error()) is True

    def test_timeout_retryable(self):
        assert is_retryable_error(asyncio.TimeoutError()) is True

    def test_retryable_status_codes(self):
        for code in (408, 429, 500, 502, 503, 504):
            assert is_retryable_error(_status_error(code)) is True

    def test_non_retryable_status_codes(self):
        for code in (400, 401, 403, 404, 422):
            assert is_retryable_error(_status_error(code)) is False

    def test_plain_exception_not_retryable(self):
        assert is_retryable_error(ValueError("boom")) is False


class TestBackoffDelay:
    def test_bounded_by_max_delay(self):
        assert backoff_delay(10, base_delay=1.0, max_delay=5.0) <= 5.0 * 1.5

    def test_grows_with_attempt(self):
        assert backoff_delay(2, 1.0, 60.0) > backoff_delay(0, 1.0, 60.0)


class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        calls = []

        async def fn():
            calls.append(1)
            return "ok"

        assert await with_retry(fn) == "ok"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_retry_then_success(self):
        attempts = []

        async def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise _request_error()
            return "ok"

        assert await with_retry(fn, base_delay=0.01, max_delay=0.02) == "ok"
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_non_retryable_returns_none_by_default(self):
        async def fn():
            raise ValueError("bad input")

        assert await with_retry(fn) is None

    @pytest.mark.asyncio
    async def test_non_retryable_reraised_when_raise_on_exhaust(self):
        async def fn():
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            await with_retry(fn, raise_on_exhaust=True)

    @pytest.mark.asyncio
    async def test_exhaustion_raises_retry_exhausted(self):
        attempts = []

        async def fn():
            attempts.append(1)
            raise _request_error()

        with pytest.raises(RetryExhausted) as exc_info:
            await with_retry(fn, max_retries=2, base_delay=0.01, max_delay=0.02, raise_on_exhaust=True)
        assert exc_info.value.attempts == 3
        assert isinstance(exc_info.value.last_error, httpx.ConnectError)
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_exhaustion_returns_none_by_default(self):
        async def fn():
            raise _request_error()

        assert await with_retry(fn, max_retries=1, base_delay=0.01, max_delay=0.02) is None

    @pytest.mark.asyncio
    async def test_custom_retry_on_predicate(self):
        attempts = []

        async def fn():
            attempts.append(1)
            raise ValueError("domain specific")

        assert await with_retry(
            fn, max_retries=2, base_delay=0.01, max_delay=0.02, retry_on=lambda e: isinstance(e, ValueError)
        ) is None
        assert len(attempts) == 3

    @pytest.mark.asyncio
    async def test_args_and_kwargs_forwarded(self):
        async def fn(a, b=0):
            return a + b

        assert await with_retry(fn, 1, b=2) == 3
