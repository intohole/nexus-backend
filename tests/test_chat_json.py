"""chat_json 组合原语守底：messages 形孪生的形状 gate+有界重试+no_cache 自愈（harness r66）。"""
import json

import pytest

from nexus import chat_json as reexported_chat_json
from nexus.llm import LLMService
from nexus.llm_utils import chat_json


class FakeLLM:
    def __init__(self, replies, fail_kwargs=None):
        self.replies = list(replies)
        self.calls = []
        self.fail_kwargs = set(fail_kwargs or [])
        self.replies_final = "garbage"

    async def chat(self, messages, system=None, **kwargs):
        for key in self.fail_kwargs:
            if len(self.calls) > 0 and not kwargs.get(key):
                raise AssertionError(f"attempt {len(self.calls) + 1} missing kwarg {key}={True}")
        self.calls.append({"messages": messages, "system": system, **kwargs})
        if self.replies:
            return self.replies.pop(0)
        return self.replies_final


MSGS = [{"role": "user", "content": "q"}]


@pytest.mark.anyio
async def test_chat_dict_shape_success():
    llm = FakeLLM([json.dumps({"a": 1})])
    result = await chat_json(llm, MSGS)
    assert result == {"a": 1}
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_chat_retries_then_succeeds_with_no_cache():
    llm = FakeLLM(["not json at all", json.dumps({"ok": True})], fail_kwargs={"no_cache"})
    result = await chat_json(llm, MSGS)
    assert result == {"ok": True}
    assert len(llm.calls) == 2
    assert "no_cache" not in llm.calls[0]
    assert llm.calls[1]["no_cache"] is True


@pytest.mark.anyio
async def test_chat_all_attempts_fail_returns_none():
    llm = FakeLLM(["garbage", "{{{"], fail_kwargs={"no_cache"})
    result = await chat_json(llm, MSGS, attempts=2)
    assert result is None
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_chat_single_attempt_no_retry():
    llm = FakeLLM(["garbage"])
    result = await chat_json(llm, MSGS, attempts=1)
    assert result is None
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_chat_kwargs_passthrough():
    llm = FakeLLM([json.dumps({"a": 1})])
    await chat_json(llm, MSGS, model="m1", namespace="Suki", timeout=120.0)
    assert llm.calls[0]["model"] == "m1"
    assert llm.calls[0]["namespace"] == "Suki"
    assert llm.calls[0]["timeout"] == 120.0


@pytest.mark.anyio
async def test_chat_list_key_gate_success():
    llm = FakeLLM([json.dumps({"variants": [{"x": 1}]})])
    result = await chat_json(llm, MSGS, root_type=list, list_key="variants")
    assert result == [{"x": 1}]


@pytest.mark.anyio
async def test_chat_dict_shape_rejects_scalar():
    llm = FakeLLM(["42", json.dumps({"a": 1})], fail_kwargs={"no_cache"})
    result = await chat_json(llm, MSGS)
    assert result == {"a": 1}
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_reexport_same_object():
    assert reexported_chat_json is chat_json


@pytest.fixture()
def svc(monkeypatch):
    service = LLMService()
    captured = []

    async def fake_chat(messages, system=None, **kwargs):
        captured.append({"messages": messages, "system": system, **kwargs})
        if len(captured) == 1:
            return " STILL THINKING "
        return json.dumps({"done": True})

    monkeypatch.setattr(service, "chat", fake_chat)
    return service, captured


@pytest.mark.anyio
async def test_method_delegates_with_json_mode_default(svc):
    service, captured = svc
    result = await service.chat_json(MSGS)
    assert result == {"done": True}
    assert len(captured) == 2
    assert captured[0]["json_mode"] is True
    assert captured[1]["no_cache"] is True


@pytest.mark.anyio
async def test_method_raise_on_error_after_retries(svc, monkeypatch):
    service, _ = svc

    async def always_garbage(messages, system=None, **kwargs):
        return "garbage"

    monkeypatch.setattr(service, "chat", always_garbage)
    with pytest.raises(ValueError, match="failed JSON shape gate"):
        await service.chat_json(MSGS)


@pytest.mark.anyio
async def test_method_sentinel_opt_out(svc, monkeypatch):
    service, captured = svc

    async def garbage_then_valid(messages, system=None, **kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            return "garbage"
        return json.dumps({"late": True})

    monkeypatch.setattr(service, "chat", garbage_then_valid)
    result = await service.chat_json(MSGS, raise_on_error=False)
    assert result == {"late": True}
    assert len(captured) == 2
