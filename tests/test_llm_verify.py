"""llm_verify 验证回路守底：schema 校验失败错误回喂修复环 + raise/哨兵语义。"""
import json as _json

import pytest
from pydantic import BaseModel

import nexus.llm_client as llm_client_mod
from nexus.llm_client import LLMJsonClient
from nexus.llm_utils import is_llm_json_error
from nexus.llm_verify import verified_json_chat

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


class ScriptedBillChat:
    """bill dict 形态 chat_fn（svc.chat _want_usage=True 通道）：返回 content+finish_reason。"""

    def __init__(self, bills: list[dict]) -> None:
        self.bills = list(bills)
        self.calls: list[dict] = []

    async def __call__(self, messages: list, json_mode: bool = False, **kwargs: object) -> dict:
        recorded = dict(kwargs)
        recorded["json_mode"] = json_mode
        self.calls.append({"messages": [dict(m) for m in messages], "kwargs": recorded})
        return self.bills.pop(0)


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
async def test_repair_round_length_amplifies_budget_and_no_cache():
    """r128：修复轮读 finish_reason——length 截断→预算放大+no_cache（r234 语义接通 schema 分支）。"""
    truncated = '{"question": "q", "options": ["a", "b", "c", "d"'
    chat = ScriptedBillChat(
        [
            {"content": truncated, "finish_reason": "length"},
            {"content": GOOD, "finish_reason": "stop"},
        ]
    )
    result = await verified_json_chat(
        chat, [{"role": "user", "content": "出题"}],
        schema=Card, max_tokens=2000, repair_no_cache=True,
    )
    assert result["options"] == ["a", "b"]
    assert len(chat.calls) == 2
    repair_kw = chat.calls[1]["kwargs"]
    assert repair_kw["max_tokens"] == 8000  # min(16000, max(2000*3, 8000))
    assert repair_kw["no_cache"] is True
    assert chat.calls[0]["kwargs"]["max_tokens"] == 2000


@pytest.mark.anyio
async def test_repair_round_other_finish_no_cache_only():
    """r128：非 length 的坏输出修复轮同预算+no_cache 绕缓存自愈（r234 分型）。"""
    chat = ScriptedBillChat(
        [
            {"content": BAD, "finish_reason": "stop"},
            {"content": GOOD, "finish_reason": "stop"},
        ]
    )
    result = await verified_json_chat(
        chat, [{"role": "user", "content": "出题"}],
        schema=Card, max_tokens=2000, repair_no_cache=True,
    )
    assert result["options"] == ["a", "b"]
    repair_kw = chat.calls[1]["kwargs"]
    assert repair_kw["max_tokens"] == 2000
    assert repair_kw["no_cache"] is True


@pytest.mark.anyio
async def test_repair_no_cache_default_off_str_compat():
    """r128：repair_no_cache 缺省 False+str 形态 chat_fn——verseCraft 零行为变化锚。"""
    chat = ScriptedChat([BAD, GOOD])
    result = await verified_json_chat(
        chat, [{"role": "user", "content": "出题"}], schema=Card, max_tokens=2000
    )
    assert result["options"] == ["a", "b"]
    assert "no_cache" not in chat.calls[1]["kwargs"]


@pytest.mark.anyio
async def test_ask_json_schema_branch_reads_finish_reason(monkeypatch):
    """r128：LLMJsonClient.ask_json schema 分支走 _want_usage bill 通道，length 修复轮放大。"""
    truncated = '{"question": "q", "options": ["a", "b", "c", "d"'
    chat = ScriptedBillChat(
        [
            {"content": truncated, "finish_reason": "length"},
            {"content": GOOD, "finish_reason": "stop"},
        ]
    )

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> dict:
        assert kwargs.get("_want_usage") is True
        return await chat(messages, json_mode=json_mode, **kwargs)

    class FakeSvc:
        pass

    FakeSvc._chat = fake_chat
    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: FakeSvc)
    client = LLMJsonClient()
    result = await client.ask_json("出题", "系统", schema=Card, max_tokens=2000)
    assert result["options"] == ["a", "b"]
    assert chat.calls[1]["kwargs"]["max_tokens"] == 8000
    assert chat.calls[1]["kwargs"]["no_cache"] is True


@pytest.mark.anyio
async def test_ask_json_multimodal_schema_repair_and_clean_cache(monkeypatch):
    """r128：多模态 schema 通道——修复轮带画面上下文回喂+schema 通过才写缓存。"""
    chat = ScriptedChat([BAD, GOOD])
    writes: list = []

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> str:
        return await chat(messages, json_mode=json_mode, **kwargs)

    class FakeSvc:
        pass

    FakeSvc._chat = fake_chat
    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: FakeSvc)
    monkeypatch.setattr(llm_client_mod, "resolve_profile", lambda p: p)
    monkeypatch.setattr(llm_client_mod, "plan_for", lambda prof: type("P", (), {"max_side": 1024, "target_tokens": 800})())
    monkeypatch.setattr(llm_client_mod, "detail_for", lambda prof, d: "low")
    monkeypatch.setattr(
        llm_client_mod, "prepare_data_url",
        lambda url, max_side, target_tokens: (url, type("M", (), {"est_tokens": 100})()),
    )

    class FakeCache:
        def get(self, key):
            return None

        def set(self, key, raw):
            writes.append((key, raw))

    monkeypatch.setattr(llm_client_mod, "get_vision_cache", lambda: FakeCache())
    monkeypatch.setattr(llm_client_mod, "build_key", lambda **kw: "k1")

    client = LLMJsonClient()
    result = await client.ask_json_multimodal(
        "看图出题", "系统", ["data:image/png;base64,AAA"],
        schema=Card, repair_attempts=1, max_tokens=2000,
    )
    assert result["options"] == ["a", "b"]
    assert len(chat.calls) == 2
    first_content = chat.calls[0]["messages"][0]["content"]
    assert isinstance(first_content, list)  # 首轮带图 content 列表
    second_convo = chat.calls[1]["messages"]
    assert second_convo[1] == {"role": "assistant", "content": BAD}
    assert "未通过结构校验" in second_convo[2]["content"]
    assert second_convo[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    # schema 通过才写缓存，坏输出不落缓存
    assert len(writes) == 1 and _json.loads(writes[0][1])["question"] == "q"


@pytest.mark.anyio
async def test_llm_json_client_schema_fallback_contract(monkeypatch):
    class FakeSvc:
        pass

    async def fake_chat(messages, json_mode=False, **kwargs: object) -> str:
        return BAD

    FakeSvc._chat = fake_chat
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

    FakeSvc._chat = fake_chat
    monkeypatch.setattr(llm_client_mod, "get_llm_service", lambda: FakeSvc)
    client = LLMJsonClient()
    result = await client.ask_json("出题", "系统", schema=Card)
    assert result["options"] == ["a", "b"]
