"""extract task_type 透传守底：LLMOptions.extra 桥是缺值层契约（S27 #26）。

resumeAI/全舰队 extract 链的 task_type 经 nexus extract → ironman LLMOptions.extra
→ provider params → 网关 apply_task_effort_default 消费；本测试钉住前两跳，
防止 extra 桥被重构 silently 断开（回归即空 task_type 又占 83%）。
"""
import time
import types

import pytest

import nexus.llm as llm_mod
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
def captured_llm(monkeypatch):
    captured = {}

    async def fake_extract(prompt, schema=None, llm=None, max_retries=2):
        captured["prompt"] = prompt
        captured["llm"] = llm
        return {"ok": True}

    async def noop_async(*args, **kwargs):
        return None

    async def fake_retry(fn, timeout, retries):
        return await fn()

    monkeypatch.setattr(llm_mod, "configure_ironman", noop_async)
    monkeypatch.setattr(llm_mod, "_preflight", noop_async)
    monkeypatch.setattr(llm_mod, "_meter", noop_async)
    monkeypatch.setattr(llm_mod, "_charge", noop_async)
    monkeypatch.setattr(llm_mod, "get_llm_circuit", lambda: _FakeCircuit())
    monkeypatch.setattr(llm_mod, "llm_telemetry", _FakeTelemetry)
    monkeypatch.setattr(llm_mod, "with_llm_retry", fake_retry)

    fake_ironman = types.ModuleType("ironman")
    fake_ironman.extract = fake_extract
    monkeypatch.setitem(__import__("sys").modules, "ironman", fake_ironman)
    return captured


@pytest.mark.anyio
async def test_extract_task_type_rides_llmoptions_extra(captured_llm):
    svc = LLMService()
    result = await svc.extract("抽取以下字段", task_type="extraction")
    assert result == {"ok": True}
    llm: LLMOptions = captured_llm["llm"]
    assert isinstance(llm, LLMOptions)
    assert llm.extra == {"task_type": "extraction"}


@pytest.mark.anyio
async def test_extract_without_task_type_keeps_extra_none(captured_llm):
    svc = LLMService()
    await svc.extract("抽取以下字段")
    llm: LLMOptions = captured_llm["llm"]
    assert llm.extra is None
