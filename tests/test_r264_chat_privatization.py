"""r264 刀B 守底：chat 私有化（包内 9 锚迁 _chat）公开 alias 契约。

- alias 同一性：chat 与 _chat 同一函数对象（全签名保真，nexus-agent
  llm_signature_probe 逐参 inspect.signature 核对面零漂移）
- 位置实参契约：llm_utils.py:372 `llm._chat(messages, ...)` 位置形态保真
- patch 面随迁：实例 _chat patch 拦截 chat_json 链（存量 6 处 patch 同口径）
"""
from __future__ import annotations

import inspect

import pytest

from nexus.llm_core import LLMCore


def test_chat_alias_is_same_function_object():
    assert LLMCore.chat is LLMCore._chat


def test_chat_alias_signature_probe_contract():
    """签名探针契约：公开名必须保留显式 messages 首位形参（禁 *args/**kwargs 滑失）。"""
    sig = inspect.signature(LLMCore.chat)
    params = list(sig.parameters)
    assert params[0] == "self"
    assert params[1] == "messages"
    assert "*args" not in params and "**kwargs" not in params


@pytest.mark.anyio
async def test_positional_call_routes_through_alias(monkeypatch):
    """位置实参调用（messages 首位）经 alias 与 _chat 同路。

    判例×2（本测试首版两踩）：
    ① 类级 `chat = _chat` 绑定原函数对象——实例 patch `_chat` 不改写 alias 分派，
       alias 面拦截须同 shadow 实例 `chat` 名。
    ② LLMCore 是单例（__new__ 缓存 cls._instance）——`LLMCore.__new__(LLMCore)`
       会把全局单例置换成裸 LLMCore 实例，后续全部 LLMService() 级联缺方法；
       裸实例必须走 object.__new__ 绕开单例缓存。
    """
    service = object.__new__(LLMCore)
    captured: list[object] = []

    async def fake_chat(messages, system=None, **kwargs):
        captured.append(messages)
        return "pong"

    monkeypatch.setattr(service, "_chat", fake_chat)
    monkeypatch.setattr(service, "chat", fake_chat)
    result = await service.chat([{"role": "user", "content": "ping"}])
    assert result == "pong"
    assert captured == [[{"role": "user", "content": "ping"}]]
