"""reasoning_effort 透传契约：extra 注入 + 客户端透传，None 时零破坏。"""
import pytest

from nexus.llm import LLMService
from nexus.llm_client import LLMJsonClient


def test_build_extra_injects_effort():
    extra = LLMService._build_extra(True, None, "ppt", "high")
    assert extra is not None
    assert extra["reasoning_effort"] == "high"
    assert extra["response_format"] == {"type": "json_object"}
    assert extra["task_type"] == "ppt"


def test_build_extra_none_effort_omits_key():
    extra = LLMService._build_extra(False, None, None, None)
    assert extra is None


def test_build_extra_effort_with_namespace():
    extra = LLMService._build_extra(False, "MiaoBi", None, "low")
    assert extra == {"namespace": "MiaoBi", "reasoning_effort": "low"}


@pytest.mark.asyncio
async def test_ask_json_forwards_effort(monkeypatch):
    captured: dict = {}

    async def fake_ask(**kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    client = LLMJsonClient(model="deepseek-flash", reasoning_effort="high")
    monkeypatch.setattr(client._svc, "chat", fake_ask)
    result = await client.ask_json(prompt="p", system="s")
    assert result == {"ok": True}
    assert captured.get("reasoning_effort") == "high"


@pytest.mark.asyncio
async def test_ask_json_no_effort_by_default(monkeypatch):
    captured: dict = {}

    async def fake_ask(**kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    client = LLMJsonClient(model="deepseek-flash")
    monkeypatch.setattr(client._svc, "chat", fake_ask)
    await client.ask_json(prompt="p", system="s")
    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
async def test_ask_json_call_level_overrides_instance(monkeypatch):
    captured: dict = {}

    async def fake_ask(**kwargs):
        captured.update(kwargs)
        return '{"ok": true}'

    client = LLMJsonClient(model="deepseek-flash", reasoning_effort="high")
    monkeypatch.setattr(client._svc, "chat", fake_ask)
    await client.ask_json(prompt="p", system="s", reasoning_effort="low")
    assert captured.get("reasoning_effort") == "low"
