import pytest

import nexus.circuit_breaker as cb
from nexus.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
    all_llm_circuits,
    get_circuit_breaker,
    get_llm_circuit,
    llm_provider_key,
    record_llm_outcome,
)


@pytest.fixture(autouse=True)
def _reset_registries():
    cb._circuit_breakers.clear()
    yield
    cb._circuit_breakers.clear()


async def _trip(provider: str, n: int = 5) -> None:
    breaker = get_llm_circuit(provider)
    for _ in range(n):
        await breaker.record_outcome(RuntimeError("boom"))


@pytest.mark.asyncio
async def test_per_provider_isolation() -> None:
    await _trip("deepseek")
    assert get_llm_circuit("deepseek").state == CircuitState.OPEN
    assert get_llm_circuit("zhipu").state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_acquire_open_rejects() -> None:
    await _trip("deepseek")
    with pytest.raises(CircuitBreakerOpenError):
        await get_llm_circuit("deepseek").acquire_call()


@pytest.mark.asyncio
async def test_acquire_half_open_reopens_after_failure() -> None:
    breaker = get_llm_circuit("deepseek")
    await _trip("deepseek")
    breaker._metrics.last_failure_time -= 60.0
    await breaker.acquire_call()
    assert breaker.state == CircuitState.HALF_OPEN
    await breaker.record_outcome(RuntimeError("still down"))
    assert breaker.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_half_open_success_threshold_closes() -> None:
    breaker = get_llm_circuit("deepseek")
    await _trip("deepseek")
    breaker._metrics.last_failure_time -= 60.0
    await breaker.acquire_call()
    await breaker.record_outcome(None)
    await breaker.acquire_call()
    await breaker.record_outcome(None)
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_record_outcome_error_provider_attribution() -> None:
    class FakeProviderError(Exception):
        provider = "zhipu"

    actual = await record_llm_outcome("default", FakeProviderError("boom"))
    assert actual == "zhipu"
    assert get_llm_circuit("zhipu").metrics.total_failures == 1
    assert get_llm_circuit("default").metrics.total_failures == 0


@pytest.mark.asyncio
async def test_record_outcome_success_response_attribution() -> None:
    class FakeResponse:
        provider = "deepseek"

    actual = await record_llm_outcome("default", None, FakeResponse())
    assert actual == "deepseek"
    assert get_llm_circuit("deepseek").metrics.total_successes == 1


@pytest.mark.asyncio
async def test_record_outcome_falls_back_to_entry_key() -> None:
    class BareResponse:
        provider = ""

    actual = await record_llm_outcome("glm", None, BareResponse())
    assert actual == "glm"
    assert get_llm_circuit("glm").metrics.total_successes == 1


def test_provider_key_resolution_chain(monkeypatch) -> None:
    class Opts:
        provider = "volc"

    assert llm_provider_key(Opts()) == "volc"

    class Bare:
        provider = None

    monkeypatch.setattr(cb, "_ironman_default_provider", lambda: "deepseek")
    assert llm_provider_key(Bare()) == "deepseek"
    monkeypatch.setattr(cb, "_ironman_default_provider", lambda: None)
    assert llm_provider_key(Bare()) == "default"
    assert llm_provider_key(None) == "default"


def test_llm_registry_does_not_leak_into_generic_names() -> None:
    breaker = get_llm_circuit("zhipu")
    assert "zhipu" not in cb._circuit_breakers
    assert "llm:zhipu" in cb._circuit_breakers
    assert get_circuit_breaker("zhipu") is not breaker


def test_generic_call_semantics_unchanged() -> None:
    calls: list[str] = []

    async def ok() -> str:
        calls.append("run")
        return "fine"

    breaker = get_circuit_breaker("plain")

    async def _run() -> str:
        return await breaker.call(ok)

    import asyncio

    assert asyncio.run(_run()) == "fine"
    assert breaker.metrics.total_successes == 1


def test_generic_call_opens_after_threshold() -> None:
    import asyncio

    async def bad() -> str:
        raise RuntimeError("down")

    async def _run() -> None:
        breaker = get_circuit_breaker("plain2", CircuitBreakerConfig(failure_threshold=2))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await breaker.call(bad)
        with pytest.raises(CircuitBreakerOpenError):
            await breaker.call(bad)

    asyncio.run(_run())


def test_all_llm_circuits_aggregates_only_llm_entries() -> None:
    get_llm_circuit("deepseek")

    async def _fail() -> None:
        breaker = get_llm_circuit("deepseek")
        for _ in range(2):
            await breaker.record_outcome(RuntimeError("boom"))

    import asyncio

    asyncio.run(_fail())
    get_circuit_breaker("other-domain")
    snapshot = all_llm_circuits()
    assert set(snapshot) == {"deepseek"}
    assert snapshot["deepseek"]["state"] == "closed"
    assert snapshot["deepseek"]["metrics"]["consecutive_failures"] == 2


def test_get_llm_circuit_requires_provider() -> None:
    with pytest.raises(TypeError):
        get_llm_circuit()  # type: ignore[call-arg]
