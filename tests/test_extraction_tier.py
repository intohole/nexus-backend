"""extraction 独立档守底（r92）：生产实证预算语义分叉——S105 归一修正.

pm 生产 14 天读数：extraction 桶 19,829 次（resumeAI 1,729 次 svc.extract 消费），
真实输出 p50=616/p95=1331/p99=2327——extract 档 300 帽不适配大结构抽取，
S105 的 extraction→extract 别名归一在预算维度不成立（伪命中：svc.extract 原
语仅 extra 标注归一，温度/预算不吃档）。r92 撤别名+立独立档（3000=p99+余量，
截断率 ~0.9%<1% 触发线）+svc.extract 原语挂 resolve_effective_budget 单源。
"""
import time
import types

import pytest

import nexus.llm_probes as llm_mod
from ironman.types import LLMOptions
from nexus.llm import LLMService
from nexus.llm_budget import (
    OutputMode,
    TASK_ALIASES,
    TASK_BUDGETS,
    canonicalize_task_type,
    resolve_effective_budget,
)


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


def test_extraction_has_own_tier() -> None:
    budget = TASK_BUDGETS["extraction"]
    assert budget.max_tokens == 3000
    assert budget.temperature == 0.2
    assert budget.output_mode == OutputMode.JSON


def test_extraction_no_longer_alias_to_extract() -> None:
    assert "extraction" not in TASK_ALIASES
    assert canonicalize_task_type("extraction") == "extraction"


def test_eval_alias_kept() -> None:
    assert canonicalize_task_type("eval") == "evaluation"


def test_extraction_tier_resolves_when_omitted() -> None:
    mt, temp, mode, thinking, _tb = resolve_effective_budget(
        "extraction", None, None, None
    )
    assert mt == 3000
    assert temp == 0.2
    assert mode == OutputMode.JSON
    assert thinking is False


def test_extraction_explicit_params_win() -> None:
    mt, temp, *_ = resolve_effective_budget("extraction", 4096, 0.1, None)
    assert mt == 4096
    assert temp == 0.1


@pytest.mark.anyio
async def test_extract_primitive_rides_tier(captured_llm) -> None:
    svc = LLMService()
    await svc.extract("抽取以下字段", task_type="extraction")
    llm: LLMOptions = captured_llm["llm"]
    assert llm.extra == {"task_type": "extraction"}
    assert llm.temperature == 0.2
    assert llm.max_tokens == 3000


@pytest.mark.anyio
async def test_extract_neutral_task_type_passes_through(captured_llm) -> None:
    svc = LLMService()
    await svc.extract("抽取以下字段", task_type="interview_report")
    llm: LLMOptions = captured_llm["llm"]
    assert llm.extra == {"task_type": "interview_report"}
    assert llm.temperature is None
    assert llm.max_tokens is None


@pytest.mark.anyio
async def test_extract_without_task_type_untouched(captured_llm) -> None:
    svc = LLMService()
    await svc.extract("抽取以下字段")
    llm: LLMOptions = captured_llm["llm"]
    assert llm.extra is None
    assert llm.temperature is None
    assert llm.max_tokens is None
