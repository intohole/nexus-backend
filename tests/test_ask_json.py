"""ask_json 组合原语守底：形状 gate 三形态+有界重试+no_cache 自愈（harness r64，教训#48 验收三件套）。"""
import json

import pytest

from nexus import ask_json as reexported_ask_json
from nexus.llm import LLMService
from nexus.llm_utils import ask_json


class FakeLLM:
    def __init__(self, replies, fail_kwargs=None):
        self.replies = list(replies)
        self.calls = []
        self.fail_kwargs = set(fail_kwargs or [])
        self.replies_final = "garbage"

    async def ask(self, prompt, system=None, **kwargs):
        for key in self.fail_kwargs:
            if len(self.calls) > 0 and not kwargs.get(key):
                raise AssertionError(f"attempt {len(self.calls) + 1} missing kwarg {key}={True}")
        self.calls.append({"prompt": prompt, "system": system, **kwargs})
        if self.replies:
            return self.replies.pop(0)
        return self.replies_final


@pytest.fixture()
def d():
    return {"prompt": "p", "system": "s"}


@pytest.mark.anyio
async def test_dict_shape_success(d):
    llm = FakeLLM([json.dumps({"a": 1})])
    result = await ask_json(llm, d["prompt"], system=d["system"])
    assert result == {"a": 1}
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_retries_then_succeeds_with_no_cache(d):
    llm = FakeLLM(["not json at all", json.dumps({"ok": True})], fail_kwargs={"no_cache"})
    result = await ask_json(llm, d["prompt"], system=d["system"])
    assert result == {"ok": True}
    assert len(llm.calls) == 2
    assert "no_cache" not in llm.calls[0]
    assert llm.calls[1]["no_cache"] is True


@pytest.mark.anyio
async def test_all_attempts_fail_returns_none(d):
    llm = FakeLLM(["garbage", "{{{"], fail_kwargs={"no_cache"})
    result = await ask_json(llm, d["prompt"], system=d["system"], attempts=2)
    assert result is None
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_single_attempt_no_retry(d):
    llm = FakeLLM(["garbage"])
    result = await ask_json(llm, d["prompt"], system=d["system"], attempts=1)
    assert result is None
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_explicit_no_cache_not_overridden(d):
    llm = FakeLLM(["garbage", json.dumps({"ok": 1})])
    result = await ask_json(llm, d["prompt"], system=d["system"], no_cache=False)
    assert result == {"ok": 1}
    assert llm.calls[1]["no_cache"] is False


@pytest.mark.anyio
async def test_list_key_gate_success(d):
    llm = FakeLLM([json.dumps({"variants": [{"x": 1}, {"x": 2}]})])
    result = await ask_json(llm, d["prompt"], system=d["system"], root_type=list, list_key="variants")
    assert result == [{"x": 1}, {"x": 2}]


@pytest.mark.anyio
async def test_list_key_envelope_fallback(d):
    llm = FakeLLM([json.dumps({"type": "json_object", "value": [{"y": 1}]})])
    result = await ask_json(llm, d["prompt"], system=d["system"], root_type=list, list_key="variants")
    assert result == [{"y": 1}]


@pytest.mark.anyio
async def test_list_key_missing_key_is_gate_failure_not_wrap(d):
    llm = FakeLLM([json.dumps({"other": 1}), json.dumps({"variants": [{"z": 1}]})], fail_kwargs={"no_cache"})
    result = await ask_json(llm, d["prompt"], system=d["system"], root_type=list, list_key="variants")
    assert result == [{"z": 1}]
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_bare_array_passthrough(d):
    llm = FakeLLM(["[1, 2, 3]"])
    result = await ask_json(llm, d["prompt"], system=d["system"], root_type=list)
    assert result == [1, 2, 3]


@pytest.mark.anyio
async def test_truncated_array_deterministic_repair_no_retry(d):
    llm = FakeLLM(['[{"a": 1}, {"b": 2'])
    result = await ask_json(llm, d["prompt"], system=d["system"], root_type=list)
    assert result == [{"a": 1}]
    assert len(llm.calls) == 1


@pytest.mark.anyio
async def test_dict_shape_rejects_scalar(d):
    llm = FakeLLM(["42", json.dumps({"a": 1})], fail_kwargs={"no_cache"})
    result = await ask_json(llm, d["prompt"], system=d["system"])
    assert result == {"a": 1}
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_reexport_same_object():
    assert reexported_ask_json is ask_json


@pytest.fixture()
def svc(monkeypatch):
    service = LLMService()
    captured = []

    async def fake_ask(prompt, system=None, **kwargs):
        captured.append({"prompt": prompt, "system": system, **kwargs})
        if len(captured) == 1:
            return " STILL THINKING "
        return json.dumps({"done": True})

    monkeypatch.setattr(service, "ask", fake_ask)
    return service, captured


@pytest.mark.anyio
async def test_method_delegates_with_json_mode_default(svc):
    service, captured = svc
    result = await service.ask_json("q")
    assert result == {"done": True}
    assert len(captured) == 2
    assert captured[0]["json_mode"] is True
    assert captured[1]["no_cache"] is True


@pytest.mark.anyio
async def test_method_raise_on_error_message(svc, monkeypatch):
    service, _ = svc

    async def always_garbage(prompt, system=None, **kwargs):
        return "garbage"

    monkeypatch.setattr(service, "ask", always_garbage)
    with pytest.raises(ValueError, match="failed JSON shape gate"):
        await service.ask_json("q")


@pytest.mark.anyio
async def test_method_sentinel_opt_out(svc, monkeypatch):
    service, _ = svc

    async def always_garbage(prompt, system=None, **kwargs):
        return "garbage"

    monkeypatch.setattr(service, "ask", always_garbage)
    result = await service.ask_json("q", raise_on_error=False)
    assert result == {"raw_response": ""}


class BillingLLM:
    """模拟支持 _want_usage 的服务：每次 wire 调用返回账单 dict（r109）。"""

    def __init__(self, contents, cached_final=False):
        self.contents = list(contents)
        self.calls = []
        self.cached_final = cached_final

    async def ask(self, prompt, system=None, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("_want_usage"):
            return self.contents.pop(0)
        content = self.contents.pop(0)
        return {
            "content": content,
            "model": "deepseek-flash",
            "input_tokens": 10 + len(self.calls),
            "output_tokens": 5,
            "total_tokens": 15 + len(self.calls),
            "cached_tokens": 0,
            "cost_usd": 0.001,
            "latency_ms": 100,
            "cached": self.cached_final and not self.contents,
        }


@pytest.mark.anyio
async def test_collect_usage_success_single_bill():
    llm = BillingLLM([json.dumps({"a": 1})])
    data, usage = await ask_json(llm, "p", system="s", collect_usage=True)
    assert data == {"a": 1}
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 11
    assert usage["output_tokens"] == 5
    assert usage["model"] == "deepseek-flash"
    assert usage["cost_usd"] == 0.001
    assert "content" not in usage["bills"][0]
    assert llm.calls[0]["_want_usage"] is True


@pytest.mark.anyio
async def test_collect_usage_retry_aggregates_bills_with_no_cache():
    llm = BillingLLM(["not json", json.dumps({"ok": 1})])
    data, usage = await ask_json(llm, "p", system="s", collect_usage=True)
    assert data == {"ok": 1}
    assert usage["calls"] == 2
    assert usage["input_tokens"] == 11 + 12
    assert len(usage["bills"]) == 2
    assert llm.calls[1]["no_cache"] is True


@pytest.mark.anyio
async def test_collect_usage_total_failure_returns_none_with_usage():
    llm = BillingLLM(["bad1", "bad2"])
    data, usage = await ask_json(llm, "p", system="s", collect_usage=True)
    assert data is None
    assert usage["calls"] == 2


@pytest.mark.anyio
async def test_collect_usage_plain_str_service_tolerated():
    class LegacyLLM:
        async def ask(self, prompt, system=None, **kwargs):
            return json.dumps({"b": 2})

    data, usage = await ask_json(LegacyLLM(), "p", system="s", collect_usage=True)
    assert data == {"b": 2}
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 0


@pytest.mark.anyio
async def test_collect_usage_list_root_shape():
    llm = BillingLLM([json.dumps([{"k": "events"}, {"k": "v2"}])])
    data, usage = await ask_json(llm, "p", system="s", collect_usage=True, root_type=list, list_key="events")
    assert data == [{"k": "events"}, {"k": "v2"}]
    assert usage["calls"] == 1


@pytest.mark.anyio
async def test_without_collect_usage_return_unchanged():
    llm = BillingLLM([json.dumps({"c": 3})])
    assert await ask_json(llm, "p", system="s") == {"c": 3}
