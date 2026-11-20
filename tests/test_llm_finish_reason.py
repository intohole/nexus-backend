"""finish_reason 透传链接通守底（S180-GAP/GAP-SO2，r234）。

链路：ironman LLMResponse.finish_reason（providers 已填）→ nexus llm_core
_usage_payload 账单面 → llm_client budget_retry 条件化（length→放大重试/
其他→同预算重试）。r50 判例「盲猜×3」变「知」的守底面。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.llm_client import LLMJsonClient
from nexus.llm_core import LLMCore
from nexus.llm_json import is_llm_json_error


class _BillSvc:
    """chat(_want_usage=True) 形态的账单面 stub：逐次返回 (bill, finish_reason)。"""

    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self._calls = list(calls)
        self.chat_kwargs: list[dict] = []

    async def chat(self, **kwargs):
        self.chat_kwargs.append(dict(kwargs))
        raw, finish_reason = self._calls.pop(0)
        return {
            "content": raw,
            "finish_reason": finish_reason,
            "model": "stub",
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "cached_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": 1,
            "provider": "stub",
            "cached": False,
        }


def _install(monkeypatch, svc: _BillSvc) -> _BillSvc:
    import nexus.llm_client as llm_client_mod

    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: svc)
    return svc


def test_usage_payload_surfaces_finish_reason():
    response = SimpleNamespace(
        content="x", model="m", usage=SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, total_tokens=2, cached_tokens=0,
        ),
        finish_reason="length", cost_usd=0.0, latency_ms=1.0,
        provider="p", cached=False,
    )
    bill = LLMCore._usage_payload("x", response)
    assert bill["finish_reason"] == "length"


def test_usage_payload_cache_hit_finish_reason_empty():
    bill = LLMCore._usage_payload("cached text", None)
    assert bill["cached"] is True
    assert bill["finish_reason"] == ""


def test_usage_payload_missing_field_defaults_empty():
    response = SimpleNamespace(
        content="x", model="m", usage=SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, total_tokens=2, cached_tokens=0,
        ),
        cost_usd=0.0, latency_ms=1.0, provider="p", cached=False,
    )
    bill = LLMCore._usage_payload("x", response)
    assert bill["finish_reason"] == ""


def test_is_parse_failed_single_source():
    assert LLMJsonClient._is_parse_failed({"raw_response": "bad"}) is True
    assert LLMJsonClient._is_parse_failed({"answer": 1}) is False
    assert LLMJsonClient._is_parse_failed({"raw_response": "bad"}) == is_llm_json_error({"raw_response": "bad"})
    assert LLMJsonClient._is_parse_failed(None) == is_llm_json_error(None)


@pytest.mark.anyio
async def test_budget_retry_amplifies_on_length(monkeypatch):
    svc = _install(monkeypatch, _BillSvc([
        ('{"raw_response": "truncated outpu"', "length"),
        ('{"answer": 1}', "stop"),
    ]))
    client = LLMJsonClient(max_tokens=2000)
    result = await client.ask_json("p", "s")
    assert result == {"answer": 1}
    assert len(svc.chat_kwargs) == 2
    assert svc.chat_kwargs[1]["max_tokens"] == 8000
    assert svc.chat_kwargs[1]["no_cache"] is True


@pytest.mark.anyio
async def test_budget_retry_same_budget_on_non_length(monkeypatch):
    svc = _install(monkeypatch, _BillSvc([
        ("模型抽风不是 JSON", "stop"),
        ('{"answer": 2}', "stop"),
    ]))
    client = LLMJsonClient(max_tokens=2000)
    result = await client.ask_json("p", "s")
    assert result == {"answer": 2}
    assert len(svc.chat_kwargs) == 2
    assert svc.chat_kwargs[1]["max_tokens"] == 2000
    assert svc.chat_kwargs[1]["no_cache"] is True


@pytest.mark.anyio
async def test_budget_retry_unknown_budget_amplifies_to_cap(monkeypatch):
    svc = _install(monkeypatch, _BillSvc([
        ("截断串", "length"),
        ('{"answer": 3}', "stop"),
    ]))
    client = LLMJsonClient(max_tokens=None)
    result = await client.ask_json("p", "s")
    assert result == {"answer": 3}
    assert svc.chat_kwargs[1]["max_tokens"] == 16000


@pytest.mark.anyio
async def test_budget_retry_no_amplify_at_cap(monkeypatch):
    svc = _install(monkeypatch, _BillSvc([
        ('{"raw_response": "truncated"', "length"),
    ]))
    client = LLMJsonClient(max_tokens=16000)
    with pytest.raises(ValueError):
        await client.ask_json("p", "s")
    assert len(svc.chat_kwargs) == 1


@pytest.mark.anyio
async def test_budget_retry_disabled_keeps_single_call(monkeypatch):
    svc = _install(monkeypatch, _BillSvc([
        ('{"raw_response": "x"', "length"),
    ]))
    client = LLMJsonClient(max_tokens=2000, budget_retry=False)
    with pytest.raises(ValueError):
        await client.ask_json("p", "s")
    assert len(svc.chat_kwargs) == 1
