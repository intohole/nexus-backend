"""LLMService.ask_json_with_usage facade 守底（harness r109）：JSON 面带账单通道。

三路径：非 schema（core 组合原语计费）/schema（verified_json_chat 修复环逐笔记账）
/失败语义（raise_on_error 与 sentinel 出口，账单聚合不丢）。克隆源=arcade
llm_usage.py+goldenStock llm_tiered.py（同语义两仓平行轮子，收归判例）。
"""
import json

import pytest
from pydantic import BaseModel

from nexus.llm import LLMService


class TinyVerdict(BaseModel):
    score: int
    note: str


def _bill(content: str, seq: int, cached: bool = False) -> dict:
    return {
        "content": content,
        "model": "deepseek-flash",
        "input_tokens": 100 + seq,
        "output_tokens": 20,
        "total_tokens": 120 + seq,
        "cached_tokens": 0,
        "cost_usd": 0.01,
        "latency_ms": 50,
        "cached": cached,
    }


@pytest.fixture()
def svc():
    return LLMService()


@pytest.mark.anyio
async def test_facade_non_schema_billed(svc, monkeypatch):
    async def fake_ask(prompt, system=None, **kwargs):
        assert kwargs.get("json_mode") is True
        assert kwargs.get("task_type") == "extraction"
        return _bill(json.dumps({"x": 1}), seq=1)

    monkeypatch.setattr(svc, "ask", fake_ask)
    data, usage = await svc.ask_json_with_usage("p", system="s", task_type="extraction")
    assert data == {"x": 1}
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 101
    assert "content" not in usage["bills"][0]


@pytest.mark.anyio
async def test_facade_schema_path_bills_each_repair(svc, monkeypatch):
    state = {"n": 0}

    async def fake_chat(**kwargs):
        state["n"] += 1
        content = json.dumps({"score": 2, "note": "ok"}) if state["n"] > 1 else "totally not json"
        return _bill(content, seq=state["n"])

    monkeypatch.setattr(svc, "chat", fake_chat)
    data, usage = await svc.ask_json_with_usage(
        "p", system="s", schema=TinyVerdict, repair_attempts=1, task_type="evaluation", max_tokens=2000,
    )
    assert data == {"score": 2, "note": "ok"}
    assert usage["calls"] == 2
    assert usage["input_tokens"] == 101 + 102


@pytest.mark.anyio
async def test_facade_schema_path_raise_on_invalid(svc, monkeypatch):
    async def fake_chat(**kwargs):
        return _bill("still not json", seq=1)

    monkeypatch.setattr(svc, "chat", fake_chat)
    with pytest.raises(ValueError):
        await svc.ask_json_with_usage("p", system="s", schema=TinyVerdict, repair_attempts=0)


@pytest.mark.anyio
async def test_facade_raise_on_error_false_sentinel_with_usage(svc, monkeypatch):
    async def fake_ask(prompt, system=None, **kwargs):
        return _bill("garbage", seq=1)

    monkeypatch.setattr(svc, "ask", fake_ask)
    data, usage = await svc.ask_json_with_usage("p", system="s", task_type="analyze", raise_on_error=False)
    assert data == {"raw_response": ""}
    assert usage["calls"] == 2


@pytest.mark.anyio
async def test_facade_root_type_list_passthrough(svc, monkeypatch):
    async def fake_ask(prompt, system=None, **kwargs):
        return _bill(json.dumps([{"k": "events"}]), seq=1)

    monkeypatch.setattr(svc, "ask", fake_ask)
    data, usage = await svc.ask_json_with_usage(
        "p", system="s", root_type=list, list_key="events", task_type="analyze",
    )
    assert data == [{"k": "events"}]
    assert usage["calls"] == 1
