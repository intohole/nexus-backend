"""r268 embed 熔断接线+失败日志 model 保真守底（r267 触发器族刀3）。"""
from __future__ import annotations

import logging

import pytest

import nexus.circuit_breaker as cb
from nexus.circuit_breaker import (
    CircuitBreakerOpenError,
    CircuitState,
    llm_provider_key,
)
from nexus.ironman_instrument import _embed_provider_key, _instrument_ironman


@pytest.fixture()
def _reset_breakers():
    cb._circuit_breakers.clear()
    yield
    cb._circuit_breakers.clear()


@pytest.fixture()
def _instrumented_env(monkeypatch):
    import ironman as ironman_mod
    import nexus.ironman_instrument as instr_mod

    original = ironman_mod.embed
    instr_mod._instrumented = False
    yield ironman_mod
    monkeypatch.setattr(ironman_mod, "embed", original, raising=False)
    instr_mod._instrumented = False


async def _trip(provider: str, n: int = 5) -> None:
    breaker = cb.get_llm_circuit(provider)
    for _ in range(n):
        await breaker.record_outcome(RuntimeError("boom"))


def test_embed_provider_key_explicit() -> None:
    assert _embed_provider_key("deepseek") == "deepseek"


def test_embed_provider_key_default_channel() -> None:
    assert _embed_provider_key(None) == llm_provider_key(None)
    assert _embed_provider_key("") == llm_provider_key(None)


@pytest.mark.asyncio
async def test_embed_open_circuit_rejects(_reset_breakers, _instrumented_env) -> None:
    calls: list = []

    async def _fake_embed(text, model=None, provider=None, dimensions=None, encoding_format=None):
        calls.append(provider)
        return [[0.1, 0.2]]

    _instrumented_env.embed = _fake_embed
    _instrument_ironman("test-app")
    await _trip("deepseek")
    with pytest.raises(CircuitBreakerOpenError):
        await _instrumented_env.embed("hello", provider="deepseek")
    assert calls == [], "熔断开启时 embed 门面不得触达底层 provider"


@pytest.mark.asyncio
async def test_embed_success_records_outcome(_reset_breakers, _instrumented_env) -> None:
    async def _fake_embed(text, model=None, provider=None, dimensions=None, encoding_format=None):
        return [[0.1, 0.2]]

    _instrumented_env.embed = _fake_embed
    _instrument_ironman("test-app")
    result = await _instrumented_env.embed("hello", provider="zhipu-emb")
    assert result == [[0.1, 0.2]]
    breaker = cb.get_llm_circuit("zhipu-emb")
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_embed_failure_keeps_model_in_log(
    _reset_breakers, _instrumented_env, caplog
) -> None:
    async def _fake_embed(text, model=None, provider=None, dimensions=None, encoding_format=None):
        raise RuntimeError("emb down")

    _instrumented_env.embed = _fake_embed
    _instrument_ironman("test-app")
    with caplog.at_level(logging.ERROR, logger="nexus.ironman.instr"):
        with pytest.raises(RuntimeError):
            await _instrumented_env.embed("hello", model="embedding-3")
    line = [r for r in caplog.records if "LLM embed failed" in r.getMessage()]
    assert line, "失败须落 error 日志"
    assert "embed:embedding-3" in line[0].getMessage(), "失败日志 model 须保真已知入参（不得恒 embed:unknown）"
