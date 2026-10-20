"""r9: llm_rate_limiter 后台通道——独立分桶 + 交互优先让行 + 配置解析。"""
from __future__ import annotations

import asyncio
import time

import pytest

from nexus.llm_rate_limiter import LLMRateLimiter


class TestBackgroundChannelIsolation:
    @pytest.mark.asyncio
    async def test_background_skips_full_interactive_window(self):
        limiter = LLMRateLimiter(rate_limit=1, period=0.5, max_concurrent=5,
                                 background_rate_limit=1, background_period=0.5)
        async with limiter.limited("interactive-1"):
            pass
        start = time.monotonic()
        async with limiter.limited("bg-1", background=True):
            pass
        assert time.monotonic() - start < 0.3
        assert limiter._bg_total_calls == 1
        assert limiter._total_calls == 1

    @pytest.mark.asyncio
    async def test_interactive_skips_full_background_window(self):
        limiter = LLMRateLimiter(rate_limit=1, period=0.5, max_concurrent=5,
                                 background_rate_limit=1, background_period=0.5)
        async with limiter.limited("bg-1", background=True):
            pass
        start = time.monotonic()
        async with limiter.limited("interactive-1"):
            pass
        assert time.monotonic() - start < 0.3

    @pytest.mark.asyncio
    async def test_background_own_window_throttles_background_only(self):
        limiter = LLMRateLimiter(rate_limit=50, period=5.0, max_concurrent=5,
                                 background_rate_limit=1, background_period=0.4)
        async with limiter.limited("bg-1", background=True):
            pass
        start = time.monotonic()
        async with limiter.limited("bg-2", background=True):
            pass
        assert time.monotonic() - start >= 0.3
        assert limiter._bg_throttled_calls == 1


class TestInteractivePriority:
    @pytest.mark.asyncio
    async def test_background_yields_while_interactive_waiting(self):
        limiter = LLMRateLimiter(rate_limit=1, period=0.4, max_concurrent=5,
                                 background_rate_limit=10, background_period=1.0)
        order: list[str] = []

        async with limiter.limited("interactive-first"):
            pass

        async def interactive_task():
            async with limiter.limited("interactive-second"):
                order.append("interactive")

        async def bg_task():
            async with limiter.limited("bg", background=True):
                order.append("background")

        inter = asyncio.create_task(interactive_task())
        await asyncio.sleep(0.05)
        bg = asyncio.create_task(bg_task())
        await asyncio.sleep(0.1)
        assert order == []
        assert limiter._interactive_waiting == 1
        await asyncio.gather(inter, bg)
        assert order == ["interactive", "background"]

    @pytest.mark.asyncio
    async def test_background_enters_freely_when_interactive_not_waiting(self):
        limiter = LLMRateLimiter(rate_limit=1, period=0.4, max_concurrent=5,
                                 background_rate_limit=10, background_period=1.0)
        async with limiter.limited("interactive-first"):
            pass
        start = time.monotonic()
        async with limiter.limited("bg", background=True):
            pass
        assert time.monotonic() - start < 0.3


class TestManualApiAndStats:
    @pytest.mark.asyncio
    async def test_manual_acquire_release_background_channel(self):
        limiter = LLMRateLimiter(rate_limit=10, period=5.0, max_concurrent=5)
        await limiter.acquire(background=True)
        assert limiter._bg_total_calls == 1
        assert limiter._total_calls == 0
        limiter.release(background=True)

    def test_stats_includes_background_fields(self):
        limiter = LLMRateLimiter(rate_limit=10, period=5.0, max_concurrent=5)
        stats = limiter.get_stats()
        assert stats["background_max_concurrent"] == 1
        assert stats["interactive_waiting"] == 0
        assert stats["background_rate_limit"] == "10/5.0s"

    def test_defaults_derive_from_main_channel(self):
        limiter = LLMRateLimiter(rate_limit=8, period=4.0, max_concurrent=3)
        assert limiter._background_rate_limit == 8
        assert limiter._background_period == 4.0
        assert limiter._background_max_concurrent == 1


class TestBackgroundConfig:
    def test_resolve_defaults_derive_from_main(self, monkeypatch):
        import nexus.llm_rate_limiter as m
        monkeypatch.delenv("LLM_BG_RATE_LIMIT", raising=False)
        monkeypatch.delenv("LLM_BG_RATE_PERIOD", raising=False)
        monkeypatch.delenv("LLM_BG_MAX_CONCURRENT", raising=False)
        assert m._resolve_background_config(12, 4.0) == (12, 4.0, 1)

    def test_env_overrides(self, monkeypatch):
        import nexus.llm_rate_limiter as m
        monkeypatch.setenv("LLM_BG_RATE_LIMIT", "4")
        monkeypatch.setenv("LLM_BG_RATE_PERIOD", "2.0")
        monkeypatch.setenv("LLM_BG_MAX_CONCURRENT", "2")
        assert m._resolve_background_config(12, 4.0) == (4, 2.0, 2)

    def test_refresh_updates_background_window_params(self, monkeypatch):
        import nexus.llm_rate_limiter as m
        monkeypatch.setattr(m, "_resolve_config", lambda: (10, 5.0, 5))
        monkeypatch.setattr(m, "_resolve_background_config", lambda r, p: (3, 2.0, 1))
        limiter = LLMRateLimiter(rate_limit=10, period=5.0, max_concurrent=5)
        limiter._config_ts = 0.0
        limiter.maybe_refresh_config()
        assert limiter._background_rate_limit == 3
        assert limiter._background_period == 2.0
