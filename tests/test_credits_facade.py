"""ironman facade 层计量+计费测试：绕过 LLMService 的调用统一归账 + 双计费去重。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pytest

import nexus.credits as credits_module
from nexus.credits import (
    CreditsInsufficientError,
    CreditsService,
    PrecheckResult,
    credits_gateway_handled,
    credits_user_scope,
    gateway_handled,
)
from nexus.ironman_instrument import (
    _detect_vision_request,
    _instrument_facade_credits,
)
from nexus.credits_gateway import GATEWAY_KIND_FEATURE


class FakeSdk:
    def __init__(self, precheck_result: dict | None = None):
        self.precheck_result = precheck_result or {
            "success": True,
            "data": {"allowed": True, "cost": 10, "balance": 500, "charge_mode": "trial", "reason": "ok"},
        }
        self.consumed: list = []
        self.precheck_calls: list = []

    async def billing_precheck(self, **kwargs) -> dict:
        self.precheck_calls.append(kwargs)
        return self.precheck_result

    async def billing_consume(self, **kwargs) -> dict:
        self.consumed.append(kwargs)
        return {"success": True, "data": {"charged": True, "balance": 490, "charge_mode": "trial"}}

    async def billing_report_meters(self, items: list) -> dict:
        return {"success": True, "data": {"inserted": len(items)}}


@pytest.fixture
def service(monkeypatch):
    svc = CreditsService()
    sdk = FakeSdk()
    monkeypatch.setattr(credits_module, "_credits_service", svc)
    monkeypatch.setattr(svc, "_sdk", lambda: sdk)
    svc._test_sdk = sdk
    yield svc
    if svc._meters._flush_task is not None:
        svc._meters._flush_task.cancel()


@dataclass
class FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 20


@dataclass
class FakeResponse:
    model: str = "glm-4.6"
    content: str = "ok"
    usage: Optional[FakeUsage] = field(default_factory=FakeUsage)


@dataclass
class FakePart:
    type: str = "image_url"


@dataclass
class FakeMsg:
    content: object = "hi"


@dataclass
class FakeRequest:
    model: str = ""
    messages: list = field(default_factory=list)


@dataclass
class FakeChunk:
    content: str = ""


def test_gateway_handled_flag_scoped():
    assert gateway_handled() is False
    with credits_gateway_handled():
        assert gateway_handled() is True
    assert gateway_handled() is False


def test_detect_vision_by_model_name():
    assert _detect_vision_request(FakeRequest(model="vision")) is True
    assert _detect_vision_request(FakeRequest(model="glm-4.6")) is False


def test_detect_vision_by_image_part():
    req = FakeRequest(model="glm-4.6", messages=[FakeMsg(content=[FakePart(type="image_url")])])
    assert _detect_vision_request(req) is True
    assert _detect_vision_request(FakeRequest(model="glm-4.6", messages=[FakeMsg(content="text")])) is False


def test_vision_feature_mapped():
    assert GATEWAY_KIND_FEATURE["vision"] == "vision"


@pytest.mark.asyncio
async def test_facade_execute_bypass_call_metered_and_charged(service, monkeypatch):
    import ironman._facade as facade_pkg
    import ironman._facade_core as facade_core
    import ironman._facade_vision as facade_vision

    async def fake_execute(request, provider_name=None):
        return FakeResponse()

    monkeypatch.setattr(facade_core, "_execute", fake_execute)
    _instrument_facade_credits("testapp")

    with credits_user_scope(7):
        result = await facade_core._execute(FakeRequest(model="glm-4.6"))
    assert result.content == "ok"
    assert len(service._test_sdk.consumed) == 1
    charge = service._test_sdk.consumed[0]
    assert charge["feature"] == "chat"
    assert charge["app_key"] == "testapp"
    assert len(service._meters._buffer) == 1
    assert service._meters._buffer[0]["kind"] == "chat"
    assert facade_pkg._execute is facade_core._execute
    assert facade_vision._execute is facade_core._execute


@pytest.mark.asyncio
async def test_facade_execute_skips_when_llm_service_handled(service, monkeypatch):
    import ironman._facade_core as facade_core

    async def fake_execute(request, provider_name=None):
        return FakeResponse()

    monkeypatch.setattr(facade_core, "_execute", fake_execute)
    _instrument_facade_credits("testapp")

    with credits_user_scope(7), credits_gateway_handled():
        await facade_core._execute(FakeRequest(model="glm-4.6"))
    assert not service._test_sdk.consumed
    assert not service._meters._buffer


@pytest.mark.asyncio
async def test_facade_vision_request_charged_as_vision(service, monkeypatch):
    import ironman._facade_core as facade_core

    async def fake_execute(request, provider_name=None):
        return FakeResponse(model="vision")

    monkeypatch.setattr(facade_core, "_execute", fake_execute)
    _instrument_facade_credits("testapp")

    with credits_user_scope(7):
        await facade_core._execute(
            FakeRequest(model="vision", messages=[FakeMsg(content=[FakePart()])])
        )
    assert service._test_sdk.consumed[0]["feature"] == "vision"


@pytest.mark.asyncio
async def test_facade_execute_preflight_blocks_formal(service, monkeypatch):
    import ironman._facade_core as facade_core

    async def fake_execute(request, provider_name=None):
        return FakeResponse()

    async def fake_preflight(kind, model=""):
        raise CreditsInsufficientError("积分余额不足，请充值后重试")

    monkeypatch.setattr(facade_core, "_execute", fake_execute)
    monkeypatch.setattr(
        type(service), "gateway_preflight",
        lambda self, kind, model="": fake_preflight(kind, model),
    )
    _instrument_facade_credits("testapp")

    with pytest.raises(CreditsInsufficientError), credits_user_scope(7):
        await facade_core._execute(FakeRequest(model="glm-4.6"))
    assert not service._test_sdk.consumed


@pytest.mark.asyncio
async def test_facade_execute_preflight_fail_open(service, monkeypatch):
    import ironman._facade_core as facade_core

    async def fake_execute(request, provider_name=None):
        return FakeResponse()

    async def broken_preflight(kind, model=""):
        raise RuntimeError("uc down")

    monkeypatch.setattr(facade_core, "_execute", fake_execute)
    monkeypatch.setattr(
        type(service), "gateway_preflight",
        lambda self, kind, model="": broken_preflight(kind, model),
    )
    _instrument_facade_credits("testapp")

    with credits_user_scope(7):
        result = await facade_core._execute(FakeRequest(model="glm-4.6"))
    assert result.content == "ok"
    assert len(service._test_sdk.consumed) == 1


@pytest.mark.asyncio
async def test_stream_engine_charges_only_when_produced(service, monkeypatch):
    import ironman._facade_stream as facade_stream

    async def fake_stream(request, llm_opts, kind="chat_stream"):
        yield FakeChunk(content="hello")
        yield FakeChunk(content="")

    monkeypatch.setattr(facade_stream, "_stream_engine", fake_stream)
    _instrument_facade_credits("testapp")

    out = []
    with credits_user_scope(7):
        async for chunk in facade_stream._stream_engine(FakeRequest(model="glm-4.6"), None, "chat_stream"):
            out.append(chunk.content)
    assert out == ["hello", ""]
    assert len(service._test_sdk.consumed) == 1
    assert service._test_sdk.consumed[0]["feature"] == "chat"

    async def empty_stream(request, llm_opts, kind="chat_stream"):
        yield FakeChunk(content="")
        return

    monkeypatch.setattr(facade_stream, "_stream_engine", empty_stream)
    _instrument_facade_credits("testapp2")
    with credits_user_scope(7):
        async for _ in facade_stream._stream_engine(FakeRequest(model="glm-4.6"), None, "chat_stream"):
            pass
    assert len(service._test_sdk.consumed) == 1


@pytest.mark.asyncio
async def test_stream_engine_skips_when_handled(service, monkeypatch):
    import ironman._facade_stream as facade_stream

    async def fake_stream(request, llm_opts, kind="chat_stream"):
        yield FakeChunk(content="x")

    monkeypatch.setattr(facade_stream, "_stream_engine", fake_stream)
    _instrument_facade_credits("testapp")

    with credits_user_scope(7), credits_gateway_handled():
        async for _ in facade_stream._stream_engine(FakeRequest(), None, "stream_vision"):
            pass
    assert not service._test_sdk.consumed


@pytest.mark.asyncio
async def test_gateway_preflight_attaches_structured_details(service, monkeypatch):
    from nexus.credits_gateway import GatewayBillingMixin
    async def fake_precheck(self, feature, user_id=None, app_key=None, cost_factor=1.0):
        return PrecheckResult(
            allowed=False, cost=10, balance=0, charge_mode="trial", reason="overdraft_limit"
        )

    monkeypatch.setattr(CreditsService, "precheck", fake_precheck)
    service._remember_charge_mode("trial")

    with pytest.raises(CreditsInsufficientError) as exc_info, credits_user_scope(7):
        await GatewayBillingMixin.gateway_preflight(service, "chat", model="glm-4.6")
    details = exc_info.value.details
    assert details["code"] == "overdraft_limit"
    assert details["balance"] == 0
    assert details["cost"] == 10
    assert details["wallet_url"] == "/user-center.html#wallet"
    assert exc_info.value.status_code == 402
    assert exc_info.value.error_code == "OVERDRAFT_LIMIT"


@pytest.mark.asyncio
async def test_image_service_charge_uses_cost_factor(service):
    from nexus.credits import get_credits_service

    with credits_user_scope(7):
        await get_credits_service().consume(
            "image", ref_id="image:t:1", description="AI 生图", cost_factor=3.0
        )
    charge = service._test_sdk.consumed[0]
    assert charge["feature"] == "image"
    assert charge["cost_factor"] == 3.0
