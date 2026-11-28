"""r271 cache 窗 nexus 侧守底：流式事件孪生（业务侧缓存 token 回填出口）+重试分类对账。"""

from __future__ import annotations

import asyncio

import pytest

from nexus.llm_stream import LLMStreamEventsMixin, StreamEvent


class _StubService(LLMStreamEventsMixin):
    def __init__(self) -> None:
        self.captured: dict = {}

    async def stream_chat(self, *, usage_sink=None, **kwargs) -> object:
        self.captured["usage_sink"] = usage_sink
        self.captured["kwargs"] = kwargs
        yield "你"
        yield "好"
        if usage_sink is not None:
            usage_sink.update(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cached_tokens": 80,
                    "model": "deepseek-flash",
                    "called_at": "2026-11-28T00:00:00+00:00",
                }
            )


@pytest.mark.asyncio
async def test_stream_chat_events_yields_delta_then_usage() -> None:
    svc = _StubService()
    events = [e async for e in svc.stream_chat_events(messages=[{"role": "user", "content": "hi"}], task_type="assistant")]
    kinds = [e.kind for e in events]
    assert kinds == ["delta", "delta", "usage"]
    assert "".join(e.text for e in events if e.kind == "delta") == "你好"
    usage_event = events[-1]
    assert usage_event.usage["cached_tokens"] == 80
    assert usage_event.usage["input_tokens"] == 100
    assert svc.captured["usage_sink"] is not None, "usage_sink 必须传入底层 stream_chat"


@pytest.mark.asyncio
async def test_stream_chat_events_no_usage_frame_no_usage_event() -> None:
    class _NoUsage(LLMStreamEventsMixin):
        async def stream_chat(self, *, usage_sink=None, **kwargs):
            yield "仅文本"

    svc = _NoUsage()
    events = [e async for e in svc.stream_chat_events(messages=[{"role": "user", "content": "hi"}])]
    assert [e.kind for e in events] == ["delta"], "上游无 usage 帧=usage 事件诚实缺席"


@pytest.mark.asyncio
async def test_stream_chat_forwards_task_type() -> None:
    svc = _StubService()
    _ = [e async for e in svc.stream_chat_events(messages=[], task_type="extraction", feature="f1")]
    assert svc.captured["kwargs"]["task_type"] == "extraction"
    assert svc.captured["kwargs"]["feature"] == "f1"


def test_retry_classifier_respects_retryable_false() -> None:
    """r271 刀3b 对账：ironman 解析坏行 ProviderError(retryable=False) 不再保守重试。"""
    from ironman.errors import ProviderError
    from nexus.llm_utils import is_retryable_error

    parse_err = ProviderError(provider="deepseek", message="malformed provider response", retryable=False)
    assert is_retryable_error(parse_err) is False
    upstream = ProviderError(provider="deepseek", message="upstream 5xx", status_code=503)
    assert is_retryable_error(upstream) is True


def test_stream_event_shape() -> None:
    e = StreamEvent(kind="delta", text="x")
    assert e.usage is None
    u = StreamEvent(kind="usage", usage={"cached_tokens": 1})
    assert u.text == ""
