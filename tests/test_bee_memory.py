"""beeMemory 适配层守底：召回映射/快照渲染/写入透传/fail-open。"""
from __future__ import annotations

import pytest

from ironman.memory.layers import MemoryScope
from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.bee_memory import BeeMemoryBackend


class _FakeSDK:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail = False
        self.recall_payload: dict[str, object] = {
            "memories": [
                {
                    "id": "m1",
                    "content": "用户喜欢蓝色",
                    "source_app": "demo",
                    "score": 0.9,
                    "priority": "high",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
        self.snapshot_payload: dict[str, object] = {
            "memories": [{"content": "长期偏好中文", "source_app": "demo", "created_at": "2026-01-01"}]
        }

    async def recall_memories(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("recall", dict(kwargs)))
        if self.fail:
            raise RuntimeError("down")
        return self.recall_payload

    async def snapshot_memories(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("snapshot", dict(kwargs)))
        if self.fail:
            raise RuntimeError("down")
        return self.snapshot_payload

    async def add_memory(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("add", dict(kwargs)))
        if self.fail:
            raise RuntimeError("down")
        return {"success": True, "memory_id": "m2"}

    async def get_context(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("context", dict(kwargs)))
        return {"history_window": [], "memories": []}

    async def save_history(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("history", dict(kwargs)))
        return {"success": True}

    async def health_check(self) -> bool:
        return not self.fail


@pytest.mark.asyncio
async def test_recall_maps_payload_to_memory_items():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    items = await backend.recall("喜欢什么颜色", scope=MemoryScope.USER, limit=3)
    assert len(items) == 1
    assert items[0].content == "用户喜欢蓝色"
    assert items[0].scope == MemoryScope.USER
    assert items[0].key == "m1"
    assert items[0].importance == 0.9
    assert items[0].metadata["source_app"] == "demo"
    assert sdk.calls[0][0] == "recall"
    assert sdk.calls[0][1]["top_k"] == 3


@pytest.mark.asyncio
async def test_recall_fail_open_on_exception():
    sdk = _FakeSDK()
    sdk.fail = True
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    assert await backend.recall("任意") == []
    assert await backend.snapshot() == {}
    assert (await backend.add("事实"))["success"] is False
    assert await backend.health() is False


@pytest.mark.asyncio
async def test_snapshot_renders_stable_memory_block():
    backend = BeeMemoryBackend("u1", "demo", sdk=_FakeSDK())
    text = await backend.render_snapshot(max_chars=400)
    assert "长期偏好中文" in text
    assert text.startswith("以下是")


@pytest.mark.asyncio
async def test_harness_uses_bee_memory_for_recall_prompt_and_write():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, session_id="s1")
    harness = build_agent_harness(HarnessProfile(app_name="demo"), memory_backend=backend)
    items = await harness.recall("颜色")
    assert [item.content for item in items] == ["用户喜欢蓝色"]
    prompt = await harness.build_system_prompt_with_memory()
    assert "长期偏好中文" in prompt
    await harness.remember("用户偏好深色主题")
    add_calls = [call for call in sdk.calls if call[0] == "add"]
    assert add_calls
    assert add_calls[0][1]["content"] == "用户偏好深色主题"
    assert add_calls[0][1]["session_id"] == "s1"
    await harness.close()


@pytest.mark.asyncio
async def test_add_forwards_scope_session_and_channel():
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, session_id="default")
    result = await backend.add("事实", scope=MemoryScope.USER, session_id="s2", channel="chat")
    assert result["success"] is True
    call = [item for item in sdk.calls if item[0] == "add"][0][1]
    assert call["session_id"] == "s2"
    assert call["channel"] == "chat"
    assert call["app_name"] == "demo"
