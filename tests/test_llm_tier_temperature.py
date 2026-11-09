"""档位温度端到端守底（r90）：省略 temperature 的调用必须吃 TASK_BUDGETS 温度列。

签名默认 0.7 时代（r73~r89）档位温度列全档失效（#55 复发形态：守底把
0.7 显式喂进 resolver，省略参数的端到端路径零覆盖）；本守底钉住三条链：
省略+有档位→档位温度、省略+无档位→0.7 兜底、显式传参恒优先。
"""
import time
import types

import pytest

import nexus.llm_core as core_mod
from ironman.types import LLMOptions
from nexus.llm import LLMService


class _FakeCircuit:
    async def call(self, fn):
        return await fn()


class _FakeTelemetry:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        class _M:
            def record(self, *args, **kwargs):
                pass

        return (_M(), time.monotonic())

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def captured_chat(monkeypatch):
    captured = {}

    class _FakeResp:
        content = "ok"
        model = "deepseek-flash"
        cost_usd = 0.0
        latency_ms = 1.0
        provider = "test"
        cached = False
        usage = None

    async def fake_chat(messages, llm):
        captured["llm"] = llm
        return _FakeResp()

    async def fake_chat_stream(messages, llm):
        captured["llm"] = llm

        class _FakeChunk:
            content = "chunk"
            usage = None
            model = "deepseek-flash"

        yield _FakeChunk()

    async def noop_async(*args, **kwargs):
        return None

    async def fake_retry(fn, timeout, retries):
        return await fn()

    monkeypatch.setattr(core_mod, "configure_ironman", noop_async)
    monkeypatch.setattr(core_mod, "_preflight", noop_async)
    monkeypatch.setattr(core_mod, "_meter", noop_async)
    monkeypatch.setattr(core_mod, "_charge", noop_async)
    monkeypatch.setattr(core_mod, "get_llm_circuit", lambda: _FakeCircuit())
    monkeypatch.setattr(core_mod, "llm_telemetry", _FakeTelemetry)
    monkeypatch.setattr(core_mod, "with_llm_retry", fake_retry)

    fake_ironman = types.ModuleType("ironman")
    fake_ironman.chat = fake_chat
    fake_ironman.chat_stream = fake_chat_stream
    monkeypatch.setitem(__import__("sys").modules, "ironman", fake_ironman)
    return captured


@pytest.mark.anyio
async def test_chat_omitted_temperature_takes_tier_temp(captured_chat):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type="extract")
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.2


@pytest.mark.anyio
async def test_chat_omitted_temperature_summarize_tier(captured_chat):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type="summarize")
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.3


@pytest.mark.anyio
async def test_chat_omitted_temperature_creative_tier(captured_chat):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type="creative")
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.85


@pytest.mark.anyio
async def test_chat_omitted_temperature_no_task_type_falls_back(captured_chat):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}])
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.7


@pytest.mark.anyio
async def test_chat_explicit_temperature_wins(captured_chat):
    svc = LLMService()
    await svc.chat([{"role": "user", "content": "x"}], task_type="extract", temperature=0.9)
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.9


@pytest.mark.anyio
async def test_stream_chat_omitted_temperature_takes_tier_temp(captured_chat):
    svc = LLMService()
    chunks = [c async for c in svc.stream_chat([{"role": "user", "content": "x"}], task_type="extract")]
    assert chunks
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.2


@pytest.mark.anyio
async def test_ask_omitted_temperature_takes_tier_temp(captured_chat):
    svc = LLMService()
    await svc.ask("x", task_type="classify")
    llm: LLMOptions = captured_chat["llm"]
    assert llm.temperature == 0.2
