"""chat_json raise_on_error 哨兵守卫：R2 审计 R7 族的底座级守底测试。"""
import pytest

from nexus.llm import LLMService
from nexus.llm_utils import is_llm_json_error


@pytest.fixture()
def svc(monkeypatch):
    service = LLMService()

    async def fake_chat(messages, system=None, json_mode=False, **kwargs):
        return "这不是 JSON 的独白回复"

    monkeypatch.setattr(service, "chat", fake_chat)
    return service


@pytest.mark.anyio
async def test_chat_json_default_returns_sentinel(svc):
    result = await svc.chat_json([{"role": "user", "content": "q"}])
    assert is_llm_json_error(result)


@pytest.mark.anyio
async def test_chat_json_raise_on_error_raises(svc):
    with pytest.raises(ValueError, match="not valid JSON"):
        await svc.chat_json([{"role": "user", "content": "q"}], raise_on_error=True)
