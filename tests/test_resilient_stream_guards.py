"""resilient_stream 守卫三件套+alias 闸聚合收编守底（S200·r243 差距 9/10）。

差距 9：resilient_stream 曾为裸通道（无预算闸/熔断/记账），消费方
LifeCompass roadmap_engine.py:81 实挂。修复=与 resilient_ask 同构三件套：
check_budget 流前拦截+cb 三段式 acquire_call/record_outcome+流完保守记账。
差距 10：alias 熔断闸 llm_{alias} 平行命名空间迁 llm:alias/{alias}，
入 /llm-circuit 聚合面（all_llm_circuits 前缀过滤自动收编）。
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import pytest

import nexus.resilient_llm as rl
from nexus.circuit_breaker import all_llm_circuits, get_circuit_breaker
from nexus.resilient_llm import CostBudgetExceededError, CircuitBreakerOpenError


class _FakeStreamService:
    def stream_ask(self, prompt: str, **kwargs: object) -> AsyncGenerator[str, None]:
        async def _gen() -> AsyncGenerator[str, None]:
            for tok in ("a", "b", "c"):
                yield tok
        return _gen()


class _BoomStreamService:
    def stream_ask(self, prompt: str, **kwargs: object) -> AsyncGenerator[str, None]:
        async def _gen() -> AsyncGenerator[str, None]:
            yield "first"
            raise RuntimeError("midstream boom")
        return _gen()


class _SpyGuard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.recorded: list[dict[str, object]] = []

    async def check_budget(self, estimated_tokens: int = 0) -> dict[str, object]:
        return {"allowed": self.allowed, "reason": "test"}

    async def record_usage(self, **kwargs: object) -> None:
        self.recorded.append(kwargs)


class _SpyBreaker:
    def __init__(self, open_now: bool = False) -> None:
        self.open_now = open_now
        self.acquired = 0
        self.outcomes: list[object] = []

    async def acquire_call(self) -> None:
        self.acquired += 1
        if self.open_now:
            raise CircuitBreakerOpenError("circuit 'test' is OPEN")

    async def record_outcome(self, error: object) -> None:
        self.outcomes.append(error)


@pytest.mark.asyncio
async def test_stream_blocked_by_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _SpyGuard(allowed=False)
    breaker = _SpyBreaker()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeStreamService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: guard)
    monkeypatch.setattr(rl, "get_circuit_breaker", lambda name, config=None: breaker)

    chunks: list[str] = []
    with pytest.raises(CostBudgetExceededError):
        async for tok in rl.resilient_stream("hi", alias="g", max_tokens=700):
            chunks.append(tok)

    assert chunks == []
    assert breaker.acquired == 0
    assert guard.recorded == []


@pytest.mark.asyncio
async def test_stream_circuit_open_rejected_before_service(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _SpyGuard()
    breaker = _SpyBreaker(open_now=True)
    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeStreamService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: guard)
    monkeypatch.setattr(rl, "get_circuit_breaker", lambda name, config=None: breaker)

    with pytest.raises(CircuitBreakerOpenError):
        async for _tok in rl.resilient_stream("hi", alias="g"):
            pass

    assert breaker.acquired == 1
    assert guard.recorded == []


@pytest.mark.asyncio
async def test_stream_success_records_usage_and_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _SpyGuard()
    breaker = _SpyBreaker()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeStreamService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: guard)
    monkeypatch.setattr(rl, "get_circuit_breaker", lambda name, config=None: breaker)

    chunks = [tok async for tok in rl.resilient_stream("hi", alias="g", max_tokens=900)]

    assert chunks == ["a", "b", "c"]
    assert breaker.acquired == 1
    assert breaker.outcomes == [None]
    assert len(guard.recorded) == 1
    rec = guard.recorded[0]
    assert rec["prompt_tokens"] == 0
    assert rec["completion_tokens"] == 900
    assert rec["model"] == ""
    assert rec["operation"] == "llm_stream"


@pytest.mark.asyncio
async def test_stream_failure_records_breaker_no_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _SpyGuard()
    breaker = _SpyBreaker()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _BoomStreamService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: guard)
    monkeypatch.setattr(rl, "get_circuit_breaker", lambda name, config=None: breaker)

    chunks: list[str] = []
    with pytest.raises(RuntimeError, match="midstream boom"):
        async for tok in rl.resilient_stream("hi", alias="g", max_tokens=300):
            chunks.append(tok)

    assert chunks == ["first"]
    assert len(breaker.outcomes) == 1
    assert str(breaker.outcomes[0]) == "midstream boom"
    assert guard.recorded == []


@pytest.mark.asyncio
async def test_stream_client_disconnect_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _SpyGuard()
    breaker = _SpyBreaker()
    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeStreamService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: guard)
    monkeypatch.setattr(rl, "get_circuit_breaker", lambda name, config=None: breaker)

    gen = rl.resilient_stream("hi", alias="g")
    await gen.__anext__()
    await gen.aclose()

    assert breaker.outcomes == []
    assert guard.recorded == []


def test_alias_breaker_in_llm_circuit_aggregate() -> None:
    cb = get_circuit_breaker(rl._alias_circuit_name("mygroup"))
    assert f"alias/mygroup" in all_llm_circuits()


@pytest.mark.asyncio
async def test_resilient_ask_uses_aggregate_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeService:
        async def ask(self, prompt: str, **kwargs: object) -> str:
            return "ok"

    monkeypatch.setattr(rl, "get_llm_service", lambda: _FakeService())
    monkeypatch.setattr(rl, "get_cost_guard", lambda: _SpyGuard())

    await rl.resilient_ask("hello", alias="agg", retry_count=0)

    assert "alias/agg" in all_llm_circuits()


def test_no_parallel_llm_alias_namespace_left() -> None:
    asyncio.run(_drain_parallel_namespace())


async def _drain_parallel_namespace() -> None:
    cb = get_circuit_breaker(rl._alias_circuit_name("probe"))
    assert cb.name.startswith("llm:")
    assert not cb.name.startswith("llm_")
