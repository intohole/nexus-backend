"""voice_realtime WS 编排网关守底：断句/分发/轮转触发/会话上限/鉴权拒绝（假 WS，零外呼）。"""
import asyncio
import json

import pytest

from nexus.voice_realtime import (
    VoiceRealtimeConfig,
    _RealtimeSession,
    register_voice_realtime_endpoints,
)

_split_sentence = _RealtimeSession._split_sentence


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send_text(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        item = await self.inbox.get()
        if item == "__disconnect__":
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class EchoHandler:
    """一次吐完整句的同步回复流，记录收到的轮次。"""

    def __init__(self) -> None:
        self.turns: list[str] = []

    async def reply(self, user_text: str, ctx: dict):
        self.turns.append(user_text)
        yield "你好，请自我介绍。"


def _session(ws: FakeWebSocket, config: VoiceRealtimeConfig | None = None) -> _RealtimeSession:
    return _RealtimeSession(ws, EchoHandler(), config or VoiceRealtimeConfig(asr_api_key=""), {"user_id": "u1"})


def test_split_sentence_hard_punct() -> None:
    flush, rest = _split_sentence("你好。请开始")
    assert flush == "你好。"
    assert rest == "请开始"


def test_split_sentence_soft_punct_requires_length() -> None:
    short = "短，句"
    assert _split_sentence(short) == ("", short)
    long_text = "这" * 31 + "，后半句"
    flush, rest = _split_sentence(long_text)
    assert flush == "这" * 31 + "，"
    assert rest == "后半句"


def test_split_sentence_no_punct_holds() -> None:
    buf = "没有任何标点的长文本"
    assert _split_sentence(buf) == ("", buf)


@pytest.mark.asyncio
async def test_dispatch_unknown_type_sends_error() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session._dispatch({"type": "bogus"})
    assert ws.sent[-1]["type"] == "error"
    assert "未知消息类型" in ws.sent[-1]["message"]


@pytest.mark.asyncio
async def test_direct_text_below_min_chars_no_turn() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session._on_direct_text("嗯")
    await asyncio.sleep(0.05)
    assert session._turn_task is None


@pytest.mark.asyncio
async def test_direct_text_triggers_turn_and_completes() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session._on_direct_text("你好，我叫测试员。")
    assert session._turn_task is not None
    await session._turn_task
    types = [m["type"] for m in ws.sent]
    assert "thinking" in types
    assert "assistant_delta" in types
    end = next(m for m in ws.sent if m["type"] == "turn_end")
    assert "自我介绍" in end["reply"]


@pytest.mark.asyncio
async def test_asr_disabled_audio_gets_hint() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session._on_audio("AAAA")
    assert any(m["type"] == "error" and "文本模式" in m["message"] for m in ws.sent)


@pytest.mark.asyncio
async def test_interrupt_cancels_running_turn() -> None:
    ws = FakeWebSocket()
    config = VoiceRealtimeConfig(asr_api_key="")

    class SlowHandler:
        async def reply(self, user_text: str, ctx: dict):
            await asyncio.sleep(5)
            yield "迟到的回复"

    session = _RealtimeSession(ws, SlowHandler(), config, {"user_id": "u1"})
    await session._on_direct_text("开始一个很长的回答。")
    assert session._turn_task is not None and not session._turn_task.done()
    await session._interrupt(notify=True)
    assert session._turn_task is None
    assert ws.sent[-1]["type"] == "interrupted"


def test_register_rejects_unauthenticated() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    registered: dict = {}

    real_register = register_voice_realtime_endpoints

    def capture(app_, handler, config, authenticator=None, path="/api/v1/voice/realtime"):
        registered["auth"] = authenticator
        return real_register(app_, handler, config, authenticator=authenticator, path=path)

    async def deny(token: str, query: dict):
        return None

    capture(app, EchoHandler(), VoiceRealtimeConfig(), authenticator=deny)
    routes = [r.path for r in app.routes]
    assert "/api/v1/voice/realtime" in routes
    assert registered["auth"] is deny
