"""r270 刀8 守底：五面失败分类（provider 才进熔断；取消/中止/本地异常不连坐）+ gateway_executor 熔断接线。"""
from __future__ import annotations

import asyncio

import pytest

from nexus.ironman_instrument import _classify_failure


def test_cancelled_classified_not_provider():
    assert _classify_failure(asyncio.CancelledError()) == "cancelled"


def test_aborted_classified_not_provider():
    from ironman.middleware.pipeline import AbortedError

    assert _classify_failure(AbortedError("abort")) == "aborted"


@pytest.mark.parametrize("exc", [ValueError("x"), TypeError("x"), KeyError("x")])
def test_local_builtin_classified_not_provider(exc: Exception):
    assert _classify_failure(exc) == "local"


def test_pydantic_and_config_error_classified_local():
    from ironman.errors import ConfigError
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        a: int

    with pytest.raises(ValidationError) as ei:
        M(a="not-int")  # type: ignore[arg-type]
    assert _classify_failure(ei.value) == "local"
    assert _classify_failure(ConfigError("bad config")) == "local"


def test_provider_error_still_classified_provider():
    assert _classify_failure(RuntimeError("upstream 500")) == "provider"


@pytest.mark.asyncio
async def test_gateway_executor_circuit_wiring(monkeypatch):
    """第六缝顺刀：acquire_call+record_llm_outcome 接线（CircuitBreakerOpen 拒绝传播）。"""
    import nexus.harness_llm as hl

    calls: list[str] = []

    class FakeCircuit:
        async def acquire_call(self):
            calls.append("acquire")

        async def release_neutral(self):
            calls.append("release_neutral")

    async def fake_route(app):
        return None

    async def fake_facade_execute(request):
        calls.append("execute")
        return object()

    monkeypatch.setattr(hl, "ensure_gateway_route", lambda app: fake_route(app))
    import nexus.circuit_breaker as cb

    monkeypatch.setattr(cb, "get_llm_circuit", lambda key: FakeCircuit())
    monkeypatch.setattr(cb, "llm_provider_key", lambda llm: "llm:default")
    recorded: list[tuple] = []

    async def fake_record(key, error, response):
        recorded.append((key, error is not None))

    monkeypatch.setattr(cb, "record_llm_outcome", fake_record)
    import ironman._facade as facade

    monkeypatch.setattr(facade, "_execute", fake_facade_execute)

    executor = hl.gateway_executor("testapp")
    await executor(object())
    assert calls == ["acquire", "execute"]
    assert recorded == [("llm:default", False)]

    async def failing_facade_execute(request):
        calls.append("execute-fail")
        raise RuntimeError("upstream down")

    monkeypatch.setattr(facade, "_execute", failing_facade_execute)
    with pytest.raises(RuntimeError):
        await executor(object())
    # r270 评审修正③b：provider 异常才记熔断失败
    assert recorded[-1] == ("llm:default", True)

    # 本地异常不连坐（评审 P1 修正①配套用例）
    async def local_error_execute(request):
        raise ValueError("local bug")

    monkeypatch.setattr(facade, "_execute", local_error_execute)
    with pytest.raises(ValueError):
        await executor(object())
    # r270 评审修正③b：local 不记=无新条目（成功 1+provider 失败 1 后长度守恒）
    assert len(recorded) == 2

    # AbortedError 中性退出不记
    from ironman.middleware.pipeline import AbortedError

    async def aborted_execute(request):
        raise AbortedError("budget abort")

    monkeypatch.setattr(facade, "_execute", aborted_execute)
    with pytest.raises(AbortedError):
        await executor(object())
    assert len(recorded) == 2
