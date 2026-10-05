"""SSE 有界队列背压契约测试。"""
import asyncio

import pytest

from nexus.streaming import DEFAULT_SSE_QUEUE_SIZE, _sse_generator_v2


def _upstream(pulled: list, total: int):
    async def _gen():
        for i in range(total):
            pulled.append(i)
            yield f"c{i}"

    return _gen()


@pytest.mark.asyncio
async def test_bounded_queue_applies_backpressure():
    pulled: list = []
    gen = _sse_generator_v2(_upstream(pulled, 200), queue_size=4)

    first = await gen.__anext__()
    assert "c0" in first
    await asyncio.sleep(0.15)

    assert len(pulled) <= DEFAULT_SSE_QUEUE_SIZE, (
        f"背压失效：上游已被拉取 {len(pulled)} 项"
    )
    await gen.aclose()


@pytest.mark.asyncio
async def test_zero_queue_size_keeps_unbounded_semantics():
    pulled: list = []
    gen = _sse_generator_v2(_upstream(pulled, 50), queue_size=0)

    await gen.__anext__()
    await asyncio.sleep(0.1)
    assert len(pulled) == 50
    await gen.aclose()


@pytest.mark.asyncio
async def test_bounded_queue_delivers_all_chunks_and_done():
    pulled: list = []

    async def slow_consume():
        gen = _sse_generator_v2(_upstream(pulled, 30), queue_size=4)
        chunks = []
        async for frame in gen:
            chunks.append(frame)
        return chunks

    chunks = await asyncio.wait_for(slow_consume(), timeout=5)
    assert len(pulled) == 30
    assert any("done" in c for c in chunks)
    assert chunks[-1].startswith("data:")
