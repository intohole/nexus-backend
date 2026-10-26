"""r15 402 信息链贯通守底：结构化 code 判定 / recovery 全链透传 / @charged details。

背景：usercenter r15 积分优化轮——UC 精心构造的 402 结构化引导（recovery 恢复清单）
此前被 nexus 中间层丢弃，nexus-ui 恢复清单对所有应用恒空。三处修复三面守底。
"""
from __future__ import annotations

import pytest

import nexus.credits as credits_module
from nexus.credits import (
    CreditsInsufficientError,
    CreditsService,
    charged,
    credits_user_scope,
)


class FakeSdk:
    def __init__(self, consume_result=None, precheck_result=None):
        self.consume_result = consume_result or {"success": True, "data": {"charged": True, "balance": 90}}
        self.precheck_result = precheck_result or {
            "success": True, "data": {"allowed": True, "cost": 30, "balance": 1200, "charge_mode": "trial", "reason": "ok"}
        }
        self.consumed: list = []

    async def billing_precheck(self, **kwargs) -> dict:
        return self.precheck_result

    async def billing_consume(self, **kwargs) -> dict:
        self.consumed.append(kwargs)
        return self.consume_result


@pytest.fixture
def service(monkeypatch):
    svc = CreditsService()
    monkeypatch.setattr(credits_module, "_credits_service", svc)
    yield svc
    if svc._meters._flush_task is not None:
        svc._meters._flush_task.cancel()


@pytest.mark.asyncio
async def test_consume_structured_error_data_carries_recovery(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "积分余额不足，请先充值", "error_data": {
        "code": "insufficient_balance", "balance": 3, "charge_mode": "formal",
        "wallet_url": "/user-center.html#wallet",
        "recovery": {"checkin": {"available": True, "next_reward": 10}},
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume("chat", user_id=1)
    assert outcome.allowed is False
    assert outcome.reason == "insufficient_balance"
    assert outcome.raw.get("recovery") == {"checkin": {"available": True, "next_reward": 10}}
    assert outcome.raw.get("code") == "insufficient_balance"


@pytest.mark.asyncio
async def test_gateway_preflight_passes_recovery_through(service, monkeypatch):
    sdk = FakeSdk(precheck_result={"success": True, "data": {
        "allowed": False, "cost": 10, "balance": 5, "list_cost": 10,
        "charge_mode": "formal", "reason": "insufficient_balance",
        "recovery": {"claimable_tasks": {"count": 2, "points": 60}},
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    service._remember_charge_mode("formal")
    with credits_user_scope(7), pytest.raises(CreditsInsufficientError) as exc_info:
        await service.gateway_preflight("chat")
    details = exc_info.value.details
    assert details["recovery"] == {"claimable_tasks": {"count": 2, "points": 60}}
    assert details["code"] == "insufficient_balance"
    assert details["list_cost"] == 10


@pytest.mark.asyncio
async def test_charged_decorator_raises_structured_details(service, monkeypatch):
    sdk = FakeSdk({"success": False, "message": "免费体验额度已用完", "error_data": {
        "code": "overdraft_limit", "balance": 0,
        "recovery": {"next_daily_gift": {"amount": 200}},
    }})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)

    @charged("generate")
    async def action() -> str:
        return "done"

    with credits_user_scope(1), pytest.raises(CreditsInsufficientError) as exc_info:
        await action()
    assert exc_info.value.error_code == "OVERDRAFT_LIMIT"
    assert exc_info.value.details.get("recovery") == {"next_daily_gift": {"amount": 200}}
