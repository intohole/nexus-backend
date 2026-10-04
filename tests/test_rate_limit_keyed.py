"""KeyedRateLimiter 分桶限流外壳行为测试。"""
import asyncio

import pytest

from nexus.rate_limit import KeyedRateLimiter


@pytest.mark.asyncio
async def test_keys_are_isolated_buckets():
    kl = KeyedRateLimiter()
    assert await kl.is_allowed("u1", 2, 60)
    assert await kl.is_allowed("u1", 2, 60)
    assert not await kl.is_allowed("u1", 2, 60)
    assert await kl.is_allowed("u2", 2, 60)


@pytest.mark.asyncio
async def test_window_param_change_rebuilds_bucket():
    kl = KeyedRateLimiter()
    assert await kl.is_allowed("u1", 1, 60)
    assert not await kl.is_allowed("u1", 1, 60)
    assert await kl.is_allowed("u1", 1, 3600)


@pytest.mark.asyncio
async def test_retry_after_positive_when_exhausted():
    kl = KeyedRateLimiter()
    for _ in range(3):
        await kl.is_allowed("u1", 2, 60)
    assert kl.retry_after("u1", 2, 60) >= 1


@pytest.mark.asyncio
async def test_acquire_wait_mode_allows_within_budget():
    kl = KeyedRateLimiter()
    await kl.acquire("u1", 3, 60)
    await kl.acquire("u1", 3, 60)
    await kl.acquire("u1", 3, 60)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(kl.acquire("u1", 3, 60), timeout=0.3)


@pytest.mark.asyncio
async def test_reset_clears_all_buckets():
    kl = KeyedRateLimiter()
    for _ in range(2):
        await kl.is_allowed("u1", 2, 60)
    assert kl.reset() >= 1
    assert await kl.is_allowed("u1", 2, 60)
