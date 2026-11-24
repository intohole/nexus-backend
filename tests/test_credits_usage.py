"""nexus 按量计费接线测试：consume_usage 链路 + auto_charge_llm usage 优先/按次兜底 + 流式 sink。"""
from __future__ import annotations

import pytest

import nexus.credits as credits_module
from nexus.credits import CreditsService, credits_user_scope


class FakeUsageSdk:
    def __init__(self, consume_result: dict | None = None):
        self.consume_result = consume_result or {"success": True, "data": {"charged": True, "balance": 90, "charge_mode": "trial"}}
        self.consumed: list = []
        self.usage_consumed: list = []

    async def billing_consume(self, **kwargs) -> dict:
        self.consumed.append(kwargs)
        return self.consume_result

    async def billing_consume_usage(self, **kwargs) -> dict:
        self.usage_consumed.append(kwargs)
        return self.consume_result

    async def billing_precheck(self, **kwargs) -> dict:
        return {"success": True, "data": {"allowed": True, "cost": 1, "balance": 100, "charge_mode": "trial", "reason": "ok"}}

    async def billing_report_meters(self, items: list) -> dict:
        return {"success": True, "data": {"inserted": len(items)}}


@pytest.fixture
def service(monkeypatch):
    svc = CreditsService()
    monkeypatch.setattr(credits_module, "_credits_service", svc)
    yield svc
    if svc._meters._flush_task is not None:
        svc._meters._flush_task.cancel()


@pytest.mark.asyncio
async def test_consume_usage_posts_raw_material(service, monkeypatch):
    sdk = FakeUsageSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume_usage(
        "chat",
        {"model": "deepseek-flash", "input_tokens": 50_000, "output_tokens": 2_000,
         "cached_tokens": 40_000, "called_at": "2026-11-24T12:00:00"},
        user_id=1, ref_id="llm:r:1", tier="lite",
    )
    assert outcome.allowed and outcome.charged
    call = sdk.usage_consumed[0]
    assert call["model"] == "deepseek-flash"
    assert call["input_tokens"] == 50_000
    assert call["cached_tokens"] == 40_000
    assert call["tier"] == "lite"
    assert call["ref_id"] == "llm:r:1"
    assert not sdk.consumed


@pytest.mark.asyncio
async def test_consume_usage_fail_open_without_user(service):
    outcome = await service.consume_usage("chat", {"input_tokens": 10}, ref_id="r")
    assert outcome.allowed and outcome.reason == "no_user_context"


@pytest.mark.asyncio
async def test_consume_usage_overdraft_blocked(service, monkeypatch):
    sdk = FakeUsageSdk({"success": False, "message": "免费体验额度已用完（体验授信 10/10）", "error_data": {"code": "overdraft_limit"}})
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    outcome = await service.consume_usage("chat", {"input_tokens": 100}, user_id=1)
    assert outcome.allowed is False
    assert outcome.reason == "overdraft_limit"


@pytest.mark.asyncio
async def test_auto_charge_llm_prefers_usage_channel(service, monkeypatch):
    sdk = FakeUsageSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    with credits_user_scope(1):
        await service.auto_charge_llm(
            kind="chat", app_name="golden", request_id="req-1", model="deepseek-flash",
            usage={"input_tokens": 10_000, "output_tokens": 500, "cached_tokens": 0},
        )
    assert len(sdk.usage_consumed) == 1
    assert sdk.usage_consumed[0]["model"] == "deepseek-flash"
    assert sdk.usage_consumed[0]["tier"] == "lite"
    assert not sdk.consumed


@pytest.mark.asyncio
async def test_auto_charge_llm_falls_back_to_per_call(service, monkeypatch):
    sdk = FakeUsageSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    with credits_user_scope(1):
        await service.auto_charge_llm(
            kind="chat", app_name="golden", request_id="req-2", model="deepseek-flash",
            usage=None,
        )
    assert not sdk.usage_consumed
    assert len(sdk.consumed) == 1
    assert sdk.consumed[0]["cost_factor"] == 0.5


@pytest.mark.asyncio
async def test_auto_charge_llm_zero_tokens_falls_back(service, monkeypatch):
    sdk = FakeUsageSdk()
    monkeypatch.setattr(service, "_sdk", lambda: sdk)
    with credits_user_scope(1):
        await service.auto_charge_llm(
            kind="chat", app_name="golden", request_id="req-3", model="glm-4-flash",
            usage={"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0},
        )
    assert not sdk.usage_consumed
    assert len(sdk.consumed) == 1


@pytest.mark.asyncio
async def test_stream_chunks_writes_usage_sink():
    from nexus.llm_helpers import stream_chunks

    sink: dict = {}

    class Chunk:
        def __init__(self, content="", usage=None, model="m1"):
            self.content = content
            self.usage = usage
            self.model = model

    class Usage:
        def __init__(self):
            self.prompt_tokens = 1200
            self.completion_tokens = 80
            self.total_tokens = 1280
            self.cached_tokens = 900

    async def fake_stream(messages, llm):
        yield Chunk(content="你好")
        yield Chunk(content="世界", usage=Usage(), model="deepseek-flash")

    opts = None
    pieces = [p async for p in stream_chunks(fake_stream, [], opts, usage_sink=sink)]
    assert "".join(pieces) == "你好世界"
    assert sink["input_tokens"] == 1200
    assert sink["output_tokens"] == 80
    assert sink["cached_tokens"] == 900
    assert sink["model"] == "deepseek-flash"
    assert sink["called_at"]
