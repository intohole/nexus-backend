"""r272 nexus 诚实性守底：stream 重试跨 attempt usage 累计（狩猎C P2 红→绿）。

钉死：空内容重试时首轮 usage 不得被末轮覆盖漏记——provider 两轮都真实计费、
ironman 正门两轮都记账，平台侧 credits/metrics 只认末轮=漏收。attempt 内取末帧
（防流内累计帧双计），attempt 间逐字段求和。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _chunk(content="", usage=None, model="fake-model"):
    return SimpleNamespace(content=content, usage=usage, model=model)


def _usage(prompt=0, completion=0, cached=0, total=0):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        cached_tokens=cached, total_tokens=total,
    )


@pytest.mark.asyncio
async def test_stream_retry_accumulates_usage_across_attempts(monkeypatch):
    from nexus import llm_helpers

    recorded: list[dict] = []

    class _StubMetrics:
        def record(self, app, model, latency, tokens=0, error=None, cached_tokens=0, cost_usd=0.0):
            recorded.append({"tokens": tokens})

    import nexus.llm_metrics as _metrics_mod
    monkeypatch.setattr(_metrics_mod, "get_llm_metrics", lambda: _StubMetrics())

    chunks = [
        [_chunk("", usage=_usage(prompt=100, cached=10, total=110))],
        [_chunk("部分", usage=_usage(prompt=200, completion=50, cached=20, total=270))],
    ]
    calls = {"n": 0}

    async def _fake_stream(*, messages, llm):
        batch = chunks[min(calls["n"], len(chunks) - 1)]
        calls["n"] += 1
        for c in batch:
            yield c

    sink: dict = {}
    pieces = []
    async for piece in llm_helpers.stream_chunks(_fake_stream, [], object(), usage_sink=sink):
        pieces.append(piece)
    assert pieces == ["部分"]
    assert calls["n"] == 2, "空内容重试未发生，场景失真"
    assert sink["input_tokens"] == 300, f"首轮 input 漏记：{sink}"
    assert sink["output_tokens"] == 50
    assert sink["cached_tokens"] == 30
    assert recorded and recorded[0]["tokens"] == 380, f"metrics tokens 漏记：{recorded}"


@pytest.mark.asyncio
async def test_stream_single_attempt_takes_last_frame(monkeypatch):
    """单 attempt 内多 usage 帧=末帧语义（防流内累计帧双计）。"""
    from nexus import llm_helpers

    recorded: list[dict] = []

    class _StubMetrics:
        def record(self, app, model, latency, tokens=0, error=None, cached_tokens=0, cost_usd=0.0):
            recorded.append({"tokens": tokens})

    import nexus.llm_metrics as _metrics_mod
    monkeypatch.setattr(_metrics_mod, "get_llm_metrics", lambda: _StubMetrics())

    async def _fake_stream(*, messages, llm):
        yield _chunk("答", usage=_usage(prompt=10, completion=1, total=11))
        yield _chunk("", usage=_usage(prompt=10, completion=5, total=15))

    sink: dict = {}
    async for _ in llm_helpers.stream_chunks(_fake_stream, [], object(), usage_sink=sink):
        pass
    assert sink["output_tokens"] == 5, f"attempt 内应取末帧：{sink}"
    assert sink["input_tokens"] == 10
    assert recorded and recorded[0]["tokens"] == 15


@pytest.mark.asyncio
async def test_stream_chat_charge_runs_via_background_spawn(monkeypatch):
    """终审 P0-1 守底：stream_chat 正常收尾计费经 spawn_background 旁路。

    钉三面：①导入路径真实（nexus.utils.background，终审抓出的 ModuleNotFound
    生产炸面）；②usage 在场正常流计费恰好一次；③后台任务异常不上抛消费方。"""
    from nexus import llm_stream

    charges: list[dict] = []
    spawned: list[object] = []

    class _StubService:
        async def _prepare_call(self, **kwargs):
            return SimpleNamespace(model="fake-model"), None, None, "balanced"

        def stream_chat_events(self, **kwargs):  # pragma: no cover - 未走此路
            raise AssertionError

    import nexus.llm_meter as meter_mod

    async def _fake_charge(*args, **kwargs):
        charges.append(kwargs)

    monkeypatch.setattr(meter_mod, "charge", _fake_charge)
    monkeypatch.setattr(meter_mod, "preflight", _fake_charge)
    monkeypatch.setattr(meter_mod, "meter", _fake_charge)

    import nexus.utils.background as bg_mod

    def _fake_spawn(coro):
        spawned.append(coro)
        return coro

    monkeypatch.setattr(bg_mod, "spawn_background", _fake_spawn)

    service = _StubService()
    gen = llm_stream.LLMStreamMixin.stream_chat(
        service, messages=[{"role": "user", "content": "hi"}], feature="probe"
    )
    # credits_gateway_handled 上下文存在即可；打桩 stream_chunks 的上游
    async def _fake_stream_chunks(inner, msgs, opts, usage_sink=None):
        usage_sink.update({"input_tokens": 5, "output_tokens": 1, "cached_tokens": 0, "model": "fake-model"})
        yield "答案"

    monkeypatch.setattr(llm_stream, "stream_chunks", _fake_stream_chunks)

    class _NoopCredits:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import nexus.llm_stream as ls_mod

    class _DummyCreditsMod:
        @staticmethod
        def credits_gateway_handled():
            return _NoopCredits()

    monkeypatch.setattr(ls_mod, "credits_gateway_handled", _DummyCreditsMod.credits_gateway_handled)

    pieces = []
    async for piece in gen:
        pieces.append(piece)
    assert pieces == ["答案"]
    assert len(spawned) == 1, "计费未走 spawn_background 旁路"
    coro = spawned[0]
    coro.close()  # 已被我们打桩接管 charge，直接关闭防 un-awaited 警告
