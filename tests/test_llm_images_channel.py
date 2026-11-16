"""带图消息通道守底（r30 arcade 刀1-M1）：convert_messages images additive 透传 +
chat 带图绕 PromptCache（key 只拼消息文本，同 prompt 不同截图必串缓存）。

零回归双面：不带图调用路径逐字节不变（str content）；带图调用图只挂末条
user 消息尾部（图位置契约=永不进 system 前缀，保 deepseek 前缀缓存稳定）。
"""
from __future__ import annotations

import sys
import time
import types

import pytest

import nexus.llm_core as core_mod
from ironman.types import ContentPartImage, ContentPartText, Role
from nexus.llm_helpers import convert_messages

_URL_A = "data:image/png;base64,AAAA"
_URL_B = "data:image/png;base64,BBBB"


class TestConvertMessagesImages:
    def test_images_attach_to_last_user_message(self):
        out = convert_messages(
            [
                {"role": "user", "content": "第一轮"},
                {"role": "assistant", "content": "好"},
                {"role": "user", "content": "修复这份代码"},
            ],
            "系统提示",
            images=[_URL_A, _URL_B],
        )
        assert out[0].role == Role.SYSTEM and isinstance(out[0].content, str)
        assert isinstance(out[1].content, str)  # 历史轮零触碰
        last = out[-1]
        assert last.role == Role.USER
        parts = last.content
        assert isinstance(parts, list)
        assert isinstance(parts[0], ContentPartText) and parts[0].text == "修复这份代码"
        assert [p.image_url.url for p in parts[1:]] == [_URL_A, _URL_B]

    def test_no_images_keeps_plain_str(self):
        out = convert_messages([{"role": "user", "content": "q"}], "sys")
        assert out[-1].content == "q"

    def test_images_without_text_content(self):
        out = convert_messages([{"role": "user", "content": ""}], None, images=[_URL_A])
        parts = out[-1].content
        assert isinstance(parts, list)
        assert all(isinstance(p, ContentPartImage) for p in parts)

    def test_empty_url_entries_filtered(self):
        out = convert_messages([{"role": "user", "content": "q"}], None, images=["", _URL_A, None])
        parts = out[-1].content
        assert len(parts) == 2  # text + 1 张有效图


class _FakeResp:
    content = "{}"
    model = "deepseek-flash"
    cost_usd = 0.0
    latency_ms = 1.0
    provider = "test"
    cached = False
    usage = None


class _FakeCircuit:
    async def call(self, fn):
        return await fn()


class _FakeTelemetry:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        class _M:
            def record(self, *args, **kwargs):
                pass

        return (_M(), time.monotonic())

    async def __aexit__(self, *exc):
        return False


@pytest.fixture()
def captured_chat(monkeypatch):
    captured: dict = {}
    cache_calls: list = []

    async def fake_chat(messages, llm):
        captured["messages"] = messages
        return _FakeResp()

    async def noop_async(*args, **kwargs):
        return None

    async def fake_retry(fn, timeout, retries):
        return await fn()

    class _SpyCache:
        def get(self, key):
            cache_calls.append(("get", key))
            return None

        def set(self, key, value):
            cache_calls.append(("set", key))

    monkeypatch.setattr(core_mod, "configure_ironman", noop_async)
    monkeypatch.setattr(core_mod, "_preflight", noop_async)
    monkeypatch.setattr(core_mod, "_meter", noop_async)
    monkeypatch.setattr(core_mod, "_charge", noop_async)
    monkeypatch.setattr(core_mod, "get_llm_circuit", lambda: _FakeCircuit())
    monkeypatch.setattr(core_mod, "llm_telemetry", _FakeTelemetry)
    monkeypatch.setattr(core_mod, "with_llm_retry", fake_retry)
    monkeypatch.setattr(core_mod, "get_prompt_cache", lambda: _SpyCache())

    fake_ironman = types.ModuleType("ironman")
    fake_ironman.chat = fake_chat
    monkeypatch.setitem(sys.modules, "ironman", fake_ironman)
    captured["cache_calls"] = cache_calls
    return captured


@pytest.mark.anyio
async def test_chat_with_images_bypasses_prompt_cache(captured_chat):
    from nexus.llm_core import LLMCore

    core = LLMCore()
    await core.chat(
        messages=[{"role": "user", "content": "修复"}],
        system="sys",
        temperature=0.0,
        images=[_URL_A],
    )
    assert captured_chat["cache_calls"] == []  # 带图恒绕缓存
    msg = captured_chat["messages"][-1]
    parts = msg.content
    assert isinstance(parts, list)
    assert isinstance(parts[0], ContentPartText)
    assert isinstance(parts[1], ContentPartImage)


@pytest.mark.anyio
async def test_chat_without_images_still_uses_cache_path(captured_chat):
    from nexus.llm_core import LLMCore

    core = LLMCore()
    await core.chat(
        messages=[{"role": "user", "content": "q"}],
        system="sys",
        temperature=0.0,
    )
    assert captured_chat["cache_calls"]  # 零回归：无图 temp<=0 仍走缓存位
