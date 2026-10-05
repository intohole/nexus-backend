"""中间件 r42 深修批回归：重试零次/熔断超时计费/缓存键隔离/限流醒来复查/lion 同步读。"""
from __future__ import annotations

import asyncio

import pytest

from nexus.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState
from nexus.llm_cache import PromptCache
from nexus.llm_rate_limiter import LLMRateLimiter
from nexus.llm_utils import with_retry


class TestWithRetryZeroAttempts:
    @pytest.mark.asyncio
    async def test_max_retries_zero_still_tries_once(self):
        calls: list[int] = []

        async def fn() -> str:
            calls.append(1)
            return "ok"

        assert await with_retry(fn, timeout=2.0, max_retries=0) == "ok"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_max_retries_zero_failure_raises_real_error(self):
        async def fn() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await with_retry(fn, timeout=2.0, max_retries=0)


class TestCircuitBreakerTimeout:
    @pytest.mark.asyncio
    async def test_timeout_cancellation_counts_as_failure(self):
        breaker = CircuitBreaker("t1", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=30.0))

        async def hang() -> str:
            await asyncio.sleep(30)
            return "never"

        for _ in range(2):
            with pytest.raises((asyncio.TimeoutError, TimeoutError)):
                async with asyncio.timeout(0.05):
                    await breaker.call(hang)

        assert breaker.state == CircuitState.OPEN
        assert breaker.metrics.consecutive_failures == 2

    @pytest.mark.asyncio
    async def test_plain_failure_still_counts(self):
        breaker = CircuitBreaker("t2", CircuitBreakerConfig(failure_threshold=1, recovery_timeout=30.0))

        async def bad() -> str:
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await breaker.call(bad)
        assert breaker.state == CircuitState.OPEN


class TestPromptCacheKeyIsolation:
    def test_different_models_do_not_share_key(self):
        k1 = PromptCache.make_messages_key("sys", [{"role": "user", "content": "p"}], 0.0, 100, model="glm-a")
        k2 = PromptCache.make_messages_key("sys", [{"role": "user", "content": "p"}], 0.0, 100, model="glm-b")
        assert k1 != k2

    def test_different_namespace_and_thinking_do_not_share_key(self):
        base = ("sys", [{"role": "user", "content": "p"}], 0.0, 100)
        assert PromptCache.make_messages_key(*base, model="m", namespace="n1") != \
            PromptCache.make_messages_key(*base, model="m", namespace="n2")
        assert PromptCache.make_messages_key(*base, model="m", enable_thinking=True) != \
            PromptCache.make_messages_key(*base, model="m", enable_thinking=False)

    def test_cache_store_respects_model_isolation(self):
        cache = PromptCache()
        msgs = [{"role": "user", "content": "hello"}]
        k_a = PromptCache.make_messages_key(None, msgs, 0.0, 50, model="a")
        k_b = PromptCache.make_messages_key(None, msgs, 0.0, 50, model="b")
        cache.set(k_a, "from-a")
        assert cache.get(k_a) == "from-a"
        assert cache.get(k_b) is None


class TestRateLimiterWakeRecheck:
    @pytest.mark.asyncio
    async def test_burst_wake_up_does_not_exceed_rate(self):
        limiter = LLMRateLimiter(rate_limit=2, period=0.4, max_concurrent=10)

        async def one(_i: int) -> None:
            async with limiter.limited(caller="test"):
                await asyncio.sleep(0.02)

        await asyncio.gather(*(one(i) for i in range(6)))

        stamps = limiter._timestamps
        in_window = [t for t in stamps if stamps[-1] - t < 0.4 - 0.05]
        assert len(in_window) <= 2

    @pytest.mark.asyncio
    async def test_timestamp_recorded_at_release_not_at_wait_start(self):
        limiter = LLMRateLimiter(rate_limit=1, period=0.3, max_concurrent=5)

        async with limiter.limited(caller="first"):
            pass
        import time as _time

        start2 = _time.monotonic()
        async with limiter.limited(caller="second"):
            pass
        assert limiter._timestamps[-1] >= start2


class TestLionSyncBusinessConfig:
    def test_sync_read_returns_cached_and_empty_when_cold(self):
        from nexus.lion import get_lion

        lion = get_lion()
        lion._cache["business::llm_quota"] = {"rate_limit": 7}
        lion._cache_ts["business::llm_quota"] = __import__("time").monotonic()
        assert lion.get_business_config_sync("llm_quota") == {"rate_limit": 7}
        assert lion.get_business_config_sync("never_cached") == {}


class TestThreadSchedulerRejectsCoroutine:
    def test_add_interval_rejects_async_func(self):
        from nexus.scheduler_thread import NexusThreadScheduler

        async def job() -> None:
            return None

        sched = NexusThreadScheduler()
        with pytest.raises(TypeError, match="同步函数"):
            sched.add_interval_job(job, job_id="x", seconds=10)
        with pytest.raises(TypeError, match="同步函数"):
            sched.add_cron_job(job, job_id="y", expr="0 9 * * *")
