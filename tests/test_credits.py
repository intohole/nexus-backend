"""nexus.credits 单元测试：计量缓冲/fail-open 消费/用户归因。"""
from __future__ import annotations

import pytest

import nexus.credits as credits_module
from nexus.credits import (
    CreditsInsufficientError,
    PrecheckResult,
    CreditsService,
    charged,
    credits_user_scope,
    get_credits_service,
)


class FakeSdk:
    def __init__(self, consume_result: dict | None = None, raise_on_report: bool = False,
                 precheck_result: dict | None = None):
        self.consume_result = consume_result or {"success": True, "data": {"charged": True, "balance": 90}}
        self.precheck_result = precheck_result or {
            "success": True, "data": {"allowed": True, "cost": 30, "balance": 1200, "charge_mode": "trial", "reason": "ok"}
        }
        self.raise_on_report = raise_on_report
        self.reported: list = []
        self.consumed: list = []
        self.precheck_calls: int = 0

    async def billing_precheck(self, **kwargs) -> dict:
        self.precheck_calls += 1
        return self.precheck_result

    async def billing_consume(self, **kwargs) -> dict:
        self.consumed.append(kwargs)
        return self.consume_result

    async def billing_report_meters(self, items: list) -> dict:
        if self.raise_on_report:
            raise RuntimeError("uc down")
        self.reported.extend(items)
        return {"success": True, "data": {"inserted": len(items)}}


@pytest.fixture
def service(monkeypatch):
    svc = CreditsService()
    monkeypatch.setattr(credits_module, "_credits_service", svc)
    yield svc
    if svc._flush_task is not None:
        svc._flush_task.cancel()


def test_report_meter_requires_user_context(service):
    service.report_meter("chat", input_tokens=10)
    assert not service._buffer


def test_report_meter_buffers_with_explicit_user(service):
    service.report_meter("chat", user_id=1, input_tokens=10, output_tokens=5)
    assert len(service._buffer) == 1
    item = service._buffer[0]
    assert item["user_id"] == 1
    assert item["kind"] == "chat"
    assert item["meter_key"]


def test_report_meter_uses_user_scope(service):
    with credits_user_scope(42):
        service.report_meter("embed", calls=2)
    assert service._buffer[0]["user_id"] == 42


@pytest.mark.asyncio
async def test_flush_meters_posts_batches(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    for i in range(3):
        service.report_meter("chat", user_id=1, input_tokens=i)
    await service.flush_meters()
    assert len(sdk.reported) == 3
    assert not service._buffer


@pytest.mark.asyncio
async def test_flush_meters_requeues_on_failure(service, monkeypatch):
    sdk = FakeSdk(raise_on_report=True)
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service.report_meter("chat", user_id=1, input_tokens=7)
    await service.flush_meters()
    assert len(service._buffer) == 1
    assert not sdk.reported


@pytest.mark.asyncio
async def test_consume_fail_open_without_user(service):
    outcome = await service.consume("chat")
    assert outcome.allowed is True
    assert outcome.reason == "no_user_context"


@pytest.mark.asyncio
async def test_consume_fail_open_without_sdk(service, monkeypatch):
    monkeypatch.setattr(service, "_sdk", lambda: None)
    outcome = await service.consume("chat", user_id=1)
    assert outcome.allowed is True
    assert outcome.reason == "credits_disabled"


@pytest.mark.asyncio
async def test_consume_success_parses_envelope(service, monkeypatch):
    sdk = FakeSdk({"success": True, "data": {"charged": True, "balance": 90, "charge_mode": "trial"}})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume("chat", user_id=1, ref_id="r1")
    assert outcome.allowed and outcome.charged
    assert outcome.balance == 90
    assert outcome.charge_mode == "trial"
    assert sdk.consumed[0]["ref_id"] == "r1"
    assert sdk.consumed[0]["feature"] == "chat"


@pytest.mark.asyncio
async def test_consume_insufficient_blocked(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "积分余额不足，请先充值"})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume("chat", user_id=1)
    assert outcome.allowed is False
    assert outcome.reason == "insufficient_balance"


@pytest.mark.asyncio
async def test_consume_other_errors_fail_open(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "认证服务连接失败，请稍后重试"})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume("chat", user_id=1)
    assert outcome.allowed is True
    assert outcome.reason == "credits_unavailable"


@pytest.mark.asyncio
async def test_charged_decorator_blocks_on_insufficient(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "积分余额不足，请先充值"})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)

    @charged("generate")
    async def action() -> str:
        return "done"

    with credits_user_scope(1):
        with pytest.raises(CreditsInsufficientError):
            await action()


@pytest.mark.asyncio
async def test_charged_decorator_passes_on_success(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)

    @charged("generate")
    async def action() -> str:
        return "done"

    with credits_user_scope(1):
        assert await action() == "done"


def test_get_credits_service_singleton(monkeypatch):
    svc = get_credits_service()
    assert get_credits_service() is svc


@pytest.mark.asyncio
async def test_precheck_blocked_on_insufficient(service, monkeypatch):
    sdk = FakeSdk(precheck_result={"success": True, "data": {
        "allowed": False, "cost": 30, "balance": 10,
        "charge_mode": "formal", "reason": "insufficient_balance",
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.precheck("generate", user_id=1)
    assert outcome.allowed is False
    assert outcome.reason == "insufficient_balance"
    assert outcome.cost == 30


@pytest.mark.asyncio
async def test_precheck_fail_open_without_sdk(service, monkeypatch):
    monkeypatch.setattr(service, "_sdk", lambda: None)
    outcome = await service.precheck("generate", user_id=1)
    assert outcome.allowed is True
    assert outcome.reason == "credits_disabled"


@pytest.mark.asyncio
async def test_precheck_no_user_context(service):
    outcome = await service.precheck("generate")
    assert outcome.allowed is True
    assert isinstance(outcome, PrecheckResult)


def test_credits_insufficient_error_is_402_nexus_error():
    from nexus.errors import NexusError

    exc = CreditsInsufficientError("积分余额不足")
    assert isinstance(exc, NexusError)
    assert exc.status_code == 402
    assert exc.error_code == "INSUFFICIENT_CREDITS"


def test_gateway_feature_default_kinds():
    assert CreditsService.gateway_feature("chat") == "chat"
    assert CreditsService.gateway_feature("chat_stream") == "chat"
    assert CreditsService.gateway_feature("extract") == "extract"
    assert CreditsService.gateway_feature("embed") == ""
    assert CreditsService.gateway_feature("unknown") == ""


def test_gateway_feature_disabled_by_config(monkeypatch):
    monkeypatch.setattr(credits_module, "yaml_bool", lambda *a, **k: False)
    assert CreditsService.gateway_feature("chat") == ""


def test_gateway_feature_respects_kinds_filter(monkeypatch):
    monkeypatch.setattr(credits_module, "yaml_get", lambda *a, **k: "chat")
    assert CreditsService.gateway_feature("chat") == "chat"
    assert CreditsService.gateway_feature("extract") == ""


@pytest.mark.asyncio
async def test_auto_charge_llm_consumes_platform_price(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    with credits_user_scope(7):
        await service.auto_charge_llm(kind="chat", app_name="demo", request_id="req-1")
    assert len(sdk.consumed) == 1
    call = sdk.consumed[0]
    assert call["feature"] == "chat"
    assert call["app_key"] == "demo"
    assert call["ref_id"].startswith("llm:req-1:")
    assert call["description"] == "AI 对话"


@pytest.mark.asyncio
async def test_auto_charge_llm_skips_anonymous(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    await service.auto_charge_llm(kind="chat", app_name="demo", request_id="req-1")
    assert not sdk.consumed


@pytest.mark.asyncio
async def test_auto_charge_llm_never_raises_on_sdk_error(service, monkeypatch):
    class BoomSdk(FakeSdk):
        async def billing_consume(self, **kwargs):
            raise RuntimeError("uc down")

    monkeypatch.setattr(service, "_sdk", lambda: BoomSdk())
    with credits_user_scope(7):
        await service.auto_charge_llm(kind="chat", app_name="demo", request_id="req-1")


@pytest.mark.asyncio
async def test_auto_charge_llm_insufficient_does_not_raise(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "积分余额不足，请先充值"})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    with credits_user_scope(7):
        await service.auto_charge_llm(kind="extract", app_name="demo", request_id="req-2")
    assert len(sdk.consumed) == 1


@pytest.mark.asyncio
async def test_consume_caches_charge_mode(service, monkeypatch):
    sdk = FakeSdk({"success": True, "data": {"charged": True, "balance": 90, "charge_mode": "formal"}})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    await service.consume("chat", user_id=1)
    assert service._cached_charge_mode() == "formal"


@pytest.mark.asyncio
async def test_gateway_preflight_blocks_formal(service, monkeypatch):
    sdk = FakeSdk(precheck_result={"success": True, "data": {
        "allowed": False, "cost": 10, "balance": 5,
        "charge_mode": "formal", "reason": "insufficient_balance",
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("formal")
    with credits_user_scope(7), pytest.raises(CreditsInsufficientError):
        await service.gateway_preflight("chat")


@pytest.mark.asyncio
async def test_gateway_preflight_skips_when_cache_empty_or_off(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    await service.gateway_preflight("chat")
    service._remember_charge_mode("off")
    await service.gateway_preflight("chat")
    assert sdk.precheck_calls == 0


@pytest.mark.asyncio
async def test_gateway_preflight_prechecks_in_trial(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("trial")
    with credits_user_scope(7):
        await service.gateway_preflight("chat")
    assert sdk.precheck_calls == 1


@pytest.mark.asyncio
async def test_gateway_preflight_blocks_trial_overdraft_limit(service, monkeypatch):
    sdk = FakeSdk(precheck_result={"success": True, "data": {
        "allowed": False, "cost": 10, "balance": 0,
        "charge_mode": "trial", "reason": "overdraft_limit",
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("trial")
    with credits_user_scope(7), pytest.raises(CreditsInsufficientError) as exc_info:
        await service.gateway_preflight("chat")
    assert "免费体验额度已用完" in str(exc_info.value)


@pytest.mark.asyncio
async def test_consume_detects_overdraft_exhausted_message(service, monkeypatch):
    sdk = FakeSdk({"success": False,
                   "message": "免费体验额度已用完（体验授信 60/60），每日赠送积分到账后可继续使用"})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume("chat", user_id=1)
    assert outcome.allowed is False
    assert outcome.reason == "overdraft_limit"


@pytest.mark.asyncio
async def test_gateway_preflight_skips_unbilled_kind(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("formal")
    await service.gateway_preflight("embed")
    assert sdk.precheck_calls == 0


@pytest.mark.asyncio
async def test_gateway_preflight_allows_formal_with_balance(service, monkeypatch):
    sdk = FakeSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("formal")
    with credits_user_scope(7):
        await service.gateway_preflight("chat")
    assert sdk.precheck_calls == 1
