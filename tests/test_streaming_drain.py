"""断连后 drain 契约守底（aiPet r14 生产实锤驱动）。

生产事故：用户 12 秒内连点快捷提问 5 次，每次点击放弃上一个流——LLM 已完整
生成并计费，但业务生成器尾部的落库随流被取消，4 条回复凭空丢失。
契约：drain_on_disconnect=True 时，客户端断开不再取消业务流，移交 drain
兜底消费者跑到自然结束（尾部落库照常执行）；默认 False 保持断开即取消。
"""
import asyncio

from nexus.streaming import (
    DRAIN_TIMEOUT_SECONDS,
    _DRAIN_TASKS,
    _sse_generator_v2,
)


def _run(coro):
    return asyncio.run(coro)


def test_default_cancels_business_stream_on_close():
    finished = []

    async def biz():
        for i in range(100):
            yield f"chunk{i}"
            await asyncio.sleep(0.01)
        finished.append(True)

    async def scenario():
        gen = _sse_generator_v2(biz())
        it = gen.__aiter__()
        await it.__anext__()
        await gen.aclose()
        await asyncio.sleep(0.1)

    _run(scenario())
    assert finished == [], "默认语义：断开即取消，生成器尾部（落库位置）不得执行"


def test_drain_runs_business_stream_to_completion():
    finished, completed_with = [], []

    async def biz():
        for i in range(5):
            yield f"chunk{i}"
            await asyncio.sleep(0.01)
        finished.append(True)

    async def on_complete(full):
        completed_with.append(full)

    async def scenario():
        gen = _sse_generator_v2(biz(), on_complete=on_complete, drain_on_disconnect=True)
        it = gen.__aiter__()
        await it.__anext__()
        await gen.aclose()
        for _ in range(100):
            if finished:
                break
            await asyncio.sleep(0.05)
        if _DRAIN_TASKS:
            await asyncio.gather(*[asyncio.shield(t) for t in list(_DRAIN_TASKS)])

    _run(scenario())
    assert finished == [True], "drain 开启：断开后业务流必须跑到自然结束（尾部落库语义）"
    assert completed_with == ["".join(f"chunk{i}" for i in range(5))], (
        "on_complete 必须收到完整内容（断开前已消费的 + drain 补齐的）"
    )
    assert not _DRAIN_TASKS, "drain 结束后必须自摘引用，防 task 泄漏"


def test_drain_survives_slow_upstream():
    finished = []

    async def biz():
        for i in range(3):
            yield f"slow{i}"
            await asyncio.sleep(0.05)
        finished.append(True)

    async def scenario():
        gen = _sse_generator_v2(biz(), drain_on_disconnect=True)
        it = gen.__aiter__()
        await it.__anext__()
        await gen.aclose()
        for _ in range(60):
            if finished:
                break
            await asyncio.sleep(0.05)

    _run(scenario())
    assert finished == [True]
    assert not _DRAIN_TASKS


def test_drain_timeout_is_positive_budget():
    assert DRAIN_TIMEOUT_SECONDS >= 60, "drain 预算必须足以覆盖正常 LLM 长流"
