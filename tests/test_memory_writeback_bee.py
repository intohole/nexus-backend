from __future__ import annotations

import pytest
from ironman.memory.summarizer import StructuredSummarizer
from ironman.memory.writeback import MemoryTurn, MemoryWriteDecision
from ironman.types import Message, Role

from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.bee_memory import BeeMemoryBackend


class _FakeSDK:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.add_result: dict[str, object] = {"success": True, "memory_id": "m1"}
        self.fail = False

    async def add_memory(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("add", dict(kwargs)))
        if self.fail:
            raise RuntimeError("beememory down")
        return self.add_result

    async def recall_memories(self, **kwargs: object) -> dict[str, object]:
        return {"memories": []}

    async def snapshot_memories(self, **kwargs: object) -> dict[str, object]:
        return {"memories": []}

    async def get_context(self, **kwargs: object) -> dict[str, object]:
        return {}

    async def save_history(self, **kwargs: object) -> dict[str, object]:
        return {"success": True}

    async def health_check(self) -> bool:
        return True


def _turn() -> MemoryTurn:
    return MemoryTurn(
        session_id="s1",
        turn_index=2,
        user_text="记住：我在做 A 股中线，偏好低估值高股息，回撤超 10% 提醒我，先看行业景气度再看个股",
        assistant_text="已记录：A 股中线、低估值高股息偏好、回撤 10% 提醒阈值，以及先行业后个股的判断顺序。",
    )


@pytest.mark.asyncio
async def test_remember_turn_writes_composed_memory():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    report = await backend.remember_turn(_turn())
    assert report.status == "saved"
    call = [item for item in sdk.calls if item[0] == "add"][0][1]
    assert "用户:" in str(call["content"])
    assert "助手:" in str(call["content"])
    assert call["session_id"] == "s1"
    assert call["channel"] == "turn_complete"


@pytest.mark.asyncio
async def test_remember_turn_gate_can_veto():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend(
        "u1",
        "demo",
        sdk=sdk,
        write_gate=lambda turn: MemoryWriteDecision(run=False, reason="low_signal"),
    )
    report = await backend.remember_turn(_turn())
    assert report.status == "skipped"
    assert report.reason == "low_signal"
    assert sdk.calls == []


@pytest.mark.asyncio
async def test_remember_turn_dedupes_repeat_turn():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    first = await backend.remember_turn(_turn())
    second = await backend.remember_turn(_turn())
    assert first.status == "saved"
    assert second.status == "no_change"
    assert len([item for item in sdk.calls if item[0] == "add"]) == 1


@pytest.mark.asyncio
async def test_remember_turn_reports_writer_failure():
    sdk = _FakeSDK()
    sdk.fail = True
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    report = await backend.remember_turn(_turn())
    assert report.status == "failed"
    assert "beememory down" in report.error


@pytest.mark.asyncio
async def test_harness_remember_turn_and_structured_compaction():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, session_id="s1")

    async def fake_llm(prompt: str) -> str:
        return "## Goal\n继续优化持仓策略"

    harness = build_agent_harness(
        HarnessProfile(app_name="demo", memory_tokens=120),
        memory_backend=backend,
        summarizer=StructuredSummarizer(llm_call=fake_llm),
    )
    report = await harness.remember_turn(
        "记住我偏好低估值高股息，回撤超过 10% 时提醒我减仓",
        "好的，已记录你的偏好与风控阈值，后续会按此提醒。",
    )
    assert report is not None
    assert report.status == "saved"

    for index in range(6):
        await harness.memory.add(Message(role=Role.USER, content=f"第{index}轮 " + "甲" * 400))
        await harness.memory.add(Message(role=Role.ASSISTANT, content="答 " + "乙" * 400))
    history = await harness.memory.get_history()
    assert any("## Goal" in str(message.content) for message in history)
    last_report = getattr(harness.memory.working, "last_report", None)
    assert last_report is not None
    assert last_report.applied is True
    assert last_report.tokens_after < last_report.tokens_before
    await harness.close()
