"""nexus.credits 单元测试：计量缓冲/fail-open 消费/用户归因。"""
from __future__ import annotations

import pytest

import nexus.credits as credits_module
from nexus.credits import (
    CreditsInsufficientError,
    CreditsService,
    charged,
    credits_user_scope,
    get_credits_service,
)


class FakeSdk:
    def __init__(self, consume_result: dict | None = None, raise_on_report: bool = False):
        self.consume_result = consume_result or {"success": True, "data": {"charged": True, "balance": 90}}
        self.raise_on_report = raise_on_report
        self.reported: list = []
        self.consumed: list = {}

    async def billing_consume(self, **kwargs) -> dict:
        self.consumed = kwargs
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
    assert sdk.consumed["ref_id"] == "r1"
    assert sdk.consumed["feature"] == "chat"


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
