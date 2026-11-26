from __future__ import annotations

import pytest
from ironman.prompt.lifecycle import PromptLifecycle
from ironman.prompt.sections import PromptSection
from ironman.skills.evolution import SkillEvolutionReview, SkillSuggestion
from ironman.skills.source import SkillCatalog

from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.bee_memory import BeeMemoryBackend
from nexus.memory_contract import MemoryBudget


class _FakeSDK:
    def __init__(self) -> None:
        self.context_payload: dict[str, object] = {
            "history_window": [
                {"role": "user", "content": "上次我们聊到持仓分散"},
                {"role": "assistant", "content": "是的，建议先看行业景气度"},
            ],
            "history_count": 2,
            "history_total": 2,
            "memories": [
                {"content": "偏好低估值高股息", "source_app": "demo", "created_at": "2026-01-01"},
            ],
            "count": 1,
        }
        self.context_calls: list[dict[str, object]] = []

    async def get_context(self, **kwargs: object) -> dict[str, object]:
        self.context_calls.append(dict(kwargs))
        return self.context_payload

    async def add_memory(self, **kwargs: object) -> dict[str, object]:
        return {"success": True}

    async def recall_memories(self, **kwargs: object) -> dict[str, object]:
        return {"memories": []}

    async def snapshot_memories(self, **kwargs: object) -> dict[str, object]:
        return {"memories": []}

    async def save_history(self, **kwargs: object) -> dict[str, object]:
        return {"success": True}

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_read_working_context_maps_history_and_memories():
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK(), session_id="s1")
    harness = build_agent_harness(HarnessProfile(app_name="demo"), memory_backend=backend)
    context = await harness.read_working_context(history_turns=6, top_k=4)
    assert context.history_count == 2
    assert [message.content for message in context.history] == [
        "上次我们聊到持仓分散",
        "是的，建议先看行业景气度",
    ]
    assert "近期对话" in context.text
    assert "偏好低估值高股息" in context.text
    await harness.close()


@pytest.mark.asyncio
async def test_restore_working_memory_seeds_only_when_empty():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, session_id="s1")
    harness = build_agent_harness(HarnessProfile(app_name="demo"), memory_backend=backend)
    assert await harness.restore_working_memory() == 2
    assert await harness.memory.size() == 2
    assert await harness.restore_working_memory() == 0
    assert await harness.memory.size() == 2
    assert sdk.context_calls[0]["history_turns"] == 12
    await harness.close()


@pytest.mark.asyncio
async def test_working_context_reports_budget_truncation():
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK(), session_id="s1")
    harness = build_agent_harness(HarnessProfile(app_name="demo"), memory_backend=backend)
    context = await harness.read_working_context(
        budget=MemoryBudget(max_tokens=40)
    )
    assert context.truncated is True
    assert "预算截断" in context.text
    await harness.close()


@pytest.mark.asyncio
async def test_system_prompt_includes_working_context_section():
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK(), session_id="s1")
    harness = build_agent_harness(HarnessProfile(app_name="demo"), memory_backend=backend)
    prompt = await harness.build_system_prompt_with_working_context(history_turns=6)
    assert "## working_context" in prompt
    assert "上次我们聊到持仓分散" in prompt
    assert "偏好低估值高股息" in prompt
    await harness.close()


@pytest.mark.asyncio
async def test_prompt_lifecycle_notice_injected_on_change():
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK(), session_id="s1")
    harness = build_agent_harness(
        HarnessProfile(app_name="demo"),
        memory_backend=backend,
        lifecycle=PromptLifecycle(),
    )
    base = [PromptSection(name="task", content="版本一", order=30)]
    first = await harness.build_system_prompt_with_working_context(
        extra_sections=base, session_id="s1"
    )
    assert "提示词变更说明" not in first
    changed = [PromptSection(name="task", content="版本二", order=30)]
    second = await harness.build_system_prompt_with_working_context(
        extra_sections=changed, session_id="s1"
    )
    assert "提示词变更说明" in second
    assert "task" in second
    third = await harness.build_system_prompt_with_working_context(
        extra_sections=changed, session_id="s1"
    )
    assert "提示词变更说明" not in third
    await harness.close()


@pytest.mark.asyncio
async def test_harness_skill_evolution_review():
    async def proposer(turn: object, names: list[str]) -> tuple[float, list[SkillSuggestion]]:
        return 88.0, [SkillSuggestion(skill_name="resume-writer", reason="补充量化案例", score=88)]

    evolution = SkillEvolutionReview(SkillCatalog([]), proposer=proposer, review_threshold=60)
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK())
    harness = build_agent_harness(
        HarnessProfile(app_name="demo"), memory_backend=backend, evolution=evolution
    )
    report = await harness.review_skill_evolution("帮我改简历", "建议补充量化指标")
    assert report is not None
    assert report.status == "suggested"
    assert report.suggestions[0].skill_name == "resume-writer"
    await harness.close()
