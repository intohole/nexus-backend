"""beeMemory 适配层守底：召回映射/快照渲染/写入透传/fail-open。"""
from __future__ import annotations

import pytest
from ironman.memory.layers import MemoryScope

from nexus.agent_harness import HarnessProfile, build_agent_harness
from nexus.bee_memory import BeeMemoryBackend
from nexus.memory_contract import (
    MemoryBudget,
    MemoryOverflowError,
    MemoryUnavailableError,
)
from nexus.memory_media import DEFAULT_IMAGE_MEMORY_MODEL, VisionDescriber


class _FakeDescriber:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def describe(self, image: str | bytes, prompt: str = "") -> str:
        self.calls.append((image, prompt))
        return "一只橘猫趴在键盘上，屏幕显示行情图"


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
    # r256：注入块围栏化
    assert text.startswith("<memory_context")


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


@pytest.mark.asyncio
async def test_recall_fail_query_raises_when_backend_down():
    sdk = _FakeSDK()
    sdk.fail = True
    backend = BeeMemoryBackend(
        "u1", "demo", sdk=sdk, budget=MemoryBudget(failure_mode="fail_query")
    )
    with pytest.raises(MemoryUnavailableError):
        await backend.recall("任意")


@pytest.mark.asyncio
async def test_snapshot_overflow_fail_query_raises():
    sdk = _FakeSDK()
    sdk.snapshot_payload = {
        "memories": [{"content": "很长的记忆" * 40, "source_app": "other"}]
    }
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    with pytest.raises(MemoryOverflowError):
        await backend.render_snapshot(
            budget=MemoryBudget(max_tokens=5, overflow="fail_query")
        )


@pytest.mark.asyncio
async def test_add_image_composes_memory_and_forwards():
    sdk = _FakeSDK()
    describer = _FakeDescriber()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, describer=describer)
    result = await backend.add_image("data:image/png;base64,xx", prompt="记住我的猫")
    assert result["success"] is True
    assert str(result["description"]).startswith("一只橘猫")
    call = [item for item in sdk.calls if item[0] == "add"][0][1]
    assert str(call["content"]).startswith("[图片记忆]记住我的猫：")
    assert call["channel"] == "image"
    assert describer.calls[0][1] == "记住我的猫"


@pytest.mark.asyncio
async def test_vision_describer_forwards_image_model():
    class _Vision:
        def __init__(self) -> None:
            self.models: list[str | None] = []

        async def analyze(self, prompt: str, images: object, system: str | None = None, model: str | None = None) -> str:
            self.models.append(model)
            return "描述"

    vision = _Vision()
    describer = VisionDescriber(vision=vision)  # type: ignore[arg-type]
    assert await describer.describe("data:image/png;base64,xx") == "描述"
    assert vision.models == [DEFAULT_IMAGE_MEMORY_MODEL]


@pytest.mark.asyncio
async def test_bee_memory_image_model_override():
    class _Vision:
        def __init__(self) -> None:
            self.models: list[str | None] = []

        async def analyze(self, prompt: str, images: object, system: str | None = None, model: str | None = None) -> str:
            self.models.append(model)
            return "描述"

    vision = _Vision()
    sdk = _FakeSDK()
    backend = BeeMemoryBackend(
        "u1",
        "demo",
        sdk=sdk,
        describer=VisionDescriber(vision=vision, model="glm-4.6v-flash"),  # type: ignore[arg-type]
    )
    await backend.add_image("data:image/png;base64,xx")
    assert vision.models == ["glm-4.6v-flash"]


@pytest.mark.asyncio
async def test_recall_image_uses_description_as_query():
    sdk = _FakeSDK()
    describer = _FakeDescriber()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk, describer=describer)
    items = await backend.recall_image("data:image/png;base64,xx", prompt="这是什么")
    assert items
    call = [item for item in sdk.calls if item[0] == "recall"][0][1]
    assert "一只橘猫" in str(call["content"])
    assert "这是什么" in str(call["content"])
    assert call["app_name"] == "demo"


@pytest.mark.asyncio
async def test_snapshot_context_wire_keys_top_n_r257():
    """r257 契约漂移守底：snapshot/context 传输键必须为 top_n（服务端 schemas 只收 top_n，旧 top_k 被静默忽略恒走默认 10）；recall 维持 top_k。"""
    sdk = _FakeSDK()
    backend = BeeMemoryBackend("u1", "demo", sdk=sdk)
    await backend.snapshot(top_k=7)
    await backend.context(history_turns=6, top_k=5)
    await backend.recall("任意", limit=3)
    by_kind = {call[0]: call[1] for call in sdk.calls}
    assert by_kind["snapshot"]["top_n"] == 7
    assert "top_k" not in by_kind["snapshot"]
    assert by_kind["context"]["top_n"] == 5
    assert "top_k" not in by_kind["context"]
    assert by_kind["recall"]["top_k"] == 3
    assert "top_n" not in by_kind["recall"]
