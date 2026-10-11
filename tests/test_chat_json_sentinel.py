"""chat_json/ask_json raise_on_error 哨兵守卫：R2 审计 R7 族的底座级守底测试（r98 默认翻转）。"""
import pytest

from nexus.llm import LLMService
from nexus.llm_utils import is_llm_json_error


@pytest.fixture()
def svc(monkeypatch):
    service = LLMService()

    async def fake_chat(messages, system=None, json_mode=False, **kwargs):
        return "这不是 JSON 的独白回复"

    async def fake_ask(prompt, system=None, json_mode=False, **kwargs):
        return "这不是 JSON 的独白回复"

    monkeypatch.setattr(service, "chat", fake_chat)
    monkeypatch.setattr(service, "ask", fake_ask)
    return service


@pytest.mark.anyio
async def test_chat_json_default_raises(svc):
    with pytest.raises(ValueError, match="not valid JSON"):
        await svc.chat_json([{"role": "user", "content": "q"}])


@pytest.mark.anyio
async def test_chat_json_sentinel_opt_out(svc):
    result = await svc.chat_json([{"role": "user", "content": "q"}], raise_on_error=False)
    assert is_llm_json_error(result)


@pytest.mark.anyio
async def test_ask_json_default_raises(svc):
    with pytest.raises(ValueError, match="not valid JSON"):
        await svc.ask_json("q")


@pytest.mark.anyio
async def test_ask_json_sentinel_opt_out(svc):
    result = await svc.ask_json("q", raise_on_error=False)
    assert is_llm_json_error(result)
