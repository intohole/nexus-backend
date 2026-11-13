"""asr_transcribe 网络异常重试必须带指数退避（r212 BE4 实锤根治守底）。

撤守必红三态：退避 sleep 消失即红（R1 同族回潮防线）；退避存在但非递增即红。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus import asr


def _ok_payload() -> dict:
    return {"text": "hello", "duration": 1.0}


@pytest.mark.asyncio
async def test_transcribe_retries_with_backoff_on_connect_error(monkeypatch):
    calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asr.asyncio, "sleep", fake_sleep)

    async def get_promptmanager_config_ok():
        return {"base_url": "http://gw.test", "api_key": "k"}

    monkeypatch.setattr(asr, "get_promptmanager_config", get_promptmanager_config_ok)

    attempts = {"n": 0}

    class FakeResp:
        status_code = 200

        def json(self):
            return _ok_payload()

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise httpx.ConnectError("boom")
            return FakeResp()

    monkeypatch.setattr(asr.httpx, "AsyncClient", FakeClient)

    result = await asr.asr_transcribe(audio_url="http://x.test/a.mp3")

    assert result.get("text") == "hello"
    assert attempts["n"] == 2
    assert len(calls) == 1
    assert calls[0] >= 1.0


@pytest.mark.asyncio
async def test_transcribe_backoff_is_exponential(monkeypatch):
    monkeypatch.setattr(asr, "_MAX_RETRIES", 4)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asr.asyncio, "sleep", fake_sleep)

    async def get_promptmanager_config_ok():
        return {"base_url": "http://gw.test", "api_key": "k"}

    monkeypatch.setattr(asr, "get_promptmanager_config", get_promptmanager_config_ok)

    class FakeResp:
        status_code = 200

        def json(self):
            return _ok_payload()

    class AlwaysFailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(asr.httpx, "AsyncClient", AlwaysFailClient)

    result = await asr.asr_transcribe(audio_url="http://x.test/a.mp3")

    assert result.get("success") is False
    assert len(delays) == asr._MAX_RETRIES - 1
    assert delays == sorted(delays)
    assert delays[-1] > delays[0]
