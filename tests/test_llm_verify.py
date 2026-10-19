"""llm_verify 验证回路守底：schema 校验失败错误回喂修复环 + raise/哨兵语义。"""
import pytest
from pydantic import BaseModel

import nexus.llm_client as llm_client_mod
import nexus.llm_verify as llm_verify_mod
from nexus.llm_client import LLMJsonClient
from nexus.llm_utils import is_llm_json_error
from nexus.llm_verify import ask_json_verified, verified_json_chat

GOOD = '{"question": "q", "options": ["a", "b"]}'
BAD = '{"question": "q"}'
PROSE = "我觉得这个问题不需要 JSON。"


class Card(BaseModel):
    question: str
    options: list[str]


class ScriptedChat:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def __call__(self, messages: list, json_mode: bool = False, **kwargs: object) -> str:
        recorded = dict(kwargs)
        recorded["json_mode"] = json_mode
        self.calls.append({"messages": [dict(m) for m in messages], "kwargs": recorded})
        return self.outputs.pop(0)


@pytest.mark.anyio
async def test_first_pass_valid_single_call():
    chat = ScriptedChat([GOOD])
    result = await verified_json_chat(chat, [{"role": "user", "content": "出题"}], schema=Card)
    assert result == {"question": "q", "options": ["a", "b"]}
    assert len(chat.calls) == 1
    assert chat.calls[0]["kwargs"]["json_mode"] is True


@pytest.mark.anyio
async def test_schema_violation_triggers_repair_with_feedback():
    chat = ScriptedChat([BAD, GOOD])
    result = await verified_json_chat(
        chat, [{"role": "user", "content": "出题"}], schema=Card, temperature=0.1
    )
    assert result["options"] == ["a", "b"]
    assert len(chat.calls) == 2
    first_convo = chat.calls[0]["messages"]
    second_convo = chat.calls[1]["messages"]
    assert len(second_convo) == 3
    assert second_convo[0] == first_convo[0]
    assert second_convo[1] == {"role": "assistant", "content": BAD}
    assert second_convo[2]["role"] == "user"
    assert "未通过结构校验" in second_convo[2]["content"]
    assert "options" in second_convo[2]["content"]
    assert chat.calls[1]["kwargs"]["temperature"] == 0.1


@pytest.mark.anyio
async def test_prose_triggers_repair():
    chat = ScriptedChat([PROSE, GOOD])
    result = await verified_json_chat(chat, [{"role": "user", "content": "出题"}], schema=Card)
    assert result["question"] == "q"
    feedback = chat.calls[1]["messages"][2]["content"]
    assert "不是合法 JSON" in feedback


@pytest.mark.anyio
async def test_repair_exhausted_raises():
    chat = ScriptedChat([BAD, BAD])
    with pytest.raises(ValueError, match="Card"):
        await verified_json_chat(chat, [{"role": "user", "content": "出题"}], schema=Card)


@pytest.mark.anyio
async def test_repair_exhausted_sentinel_opt_out():
    chat = ScriptedChat([BAD, BAD])
    result = await verified_json_chat(
        chat, [{"role": "user", "content": "出题"}], schema=Card, raise_on_error=False
    )
    assert is_llm_json_error(result)


@pytest.mark.anyio
async def test_zero_repair_attempts_single_call(monkeypatch):
    chat = ScriptedChat([BAD])
    with pytest.raises(ValueError):
        await verified_json_chat(
            chat, [{"role": "user", "content": "出题"}], schema=Card, repair_attempts=0
        )
    assert len(chat.calls) == 1


@pytest.mark.anyio
async def test_ask_json_verified_defaults(monkeypatch):
    chat = ScriptedChat([GOOD])

    class FakeSvc:
        self_chat = chat

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> str:
        return await chat(messages, json_mode=json_mode, **kwargs)

    FakeSvc.chat = fake_chat
    monkeypatch.setattr(llm_verify_mod, "get_llm_service", lambda: FakeSvc)
    result = await ask_json_verified("出题", "你是出题官", schema=Card)
    assert result["question"] == "q"
    assert chat.calls[0]["kwargs"]["temperature"] == 0.2
    assert chat.calls[0]["kwargs"]["max_tokens"] == 1500
    assert chat.calls[0]["kwargs"]["system"] == "你是出题官"


@pytest.mark.anyio
async def test_llm_json_client_schema_fallback_contract(monkeypatch):
    class FakeSvc:
        pass

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> str:
        return BAD

    FakeSvc.chat = fake_chat
    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: FakeSvc)
    client = LLMJsonClient()
    fallback = {"question": "兜底", "options": []}
    result = await client.ask_json("出题", "系统", schema=Card, fallback=fallback)
    assert result == fallback
    with pytest.raises(ValueError, match="Card"):
        await client.ask_json("出题", "系统", schema=Card)


@pytest.mark.anyio
async def test_llm_json_client_schema_repair_success(monkeypatch):
    class FakeSvc:
        pass

    chat = ScriptedChat([BAD, GOOD])

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> str:
        return await chat(messages, json_mode=json_mode, **kwargs)

    FakeSvc.chat = fake_chat
    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: FakeSvc)
    client = LLMJsonClient()
    result = await client.ask_json("出题", "系统", schema=Card)
    assert result["options"] == ["a", "b"]
