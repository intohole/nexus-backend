"""S73 语音 WS 韧性与音频计量守底：悬死 close/心跳半开/防双连/用量记账/metrics hook。

悬死修复的正反断言：未 auth 被拒连接必须收到精确 close code 4401
（修复前 _reject 二次 accept 被 except pass 吞掉，close 永不执行，
客户端只能收到框架兜底 1000 或悬死 1006）。
"""
import asyncio
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from nexus.voice_meter import VoiceUsageMeter
from nexus.voice_realtime import (
    VoiceRealtimeConfig,
    _RealtimeSession,
    _session_conns,
    register_voice_realtime_endpoints,
)
from nexus.voice_session import _HEARTBEAT_S


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False
        self.fail_send = False

    async def send_text(self, raw: str) -> None:
        if self.fail_send:
            raise RuntimeError("half-open send failure")
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        return await self.inbox.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class EchoHandler:
    def __init__(self) -> None:
        self.turns: list[str] = []
        self.metrics_events: list[dict] = []
        self.order: list[str] = []

    async def reply(self, user_text: str, ctx: dict):
        self.turns.append(user_text)
        self.order.append("reply")
        yield "你好，请自我介绍。"

    async def on_turn_metrics(self, metrics: dict, ctx: dict) -> None:
        self.metrics_events.append(metrics)
        self.order.append("metrics")

    async def on_turn_complete(self, user_text: str, reply_text: str, ctx: dict) -> None:
        self.order.append("complete")


class FakeTTS:
    async def push_text(self, text: str) -> None:
        pass

    async def finish(self) -> None:
        pass

    async def frames(self):
        yield b"pcmaudiobytes0000"

    async def close(self) -> None:
        pass


class FakeTtsLink:
    def __init__(self, api_key: str, voice: str, sample_rate: int) -> None:
        self.enabled = True

    def schedule_warm(self) -> None:
        pass

    async def acquire(self) -> FakeTTS:
        return FakeTTS()

    async def push_or_recover(self, tts: FakeTTS, text: str):
        return tts, False

    async def close_warm(self) -> None:
        pass


def _session(ws: FakeWebSocket, handler=None, config=None, user=None) -> _RealtimeSession:
    return _RealtimeSession(
        ws, handler or EchoHandler(), config or VoiceRealtimeConfig(asr_api_key=""),
        user or {"user_id": "u1", "session_id": "s1"},
    )


@pytest.fixture(autouse=True)
def _clean_session_registries():
    yield
    _session_conns.clear()


def _client_app(auth) -> TestClient:
    app = FastAPI()
    register_voice_realtime_endpoints(
        app, EchoHandler(), VoiceRealtimeConfig(asr_api_key=""), authenticator=auth
    )
    return TestClient(app)


def test_reject_closes_with_precise_code() -> None:
    async def deny(token: str, query: dict):
        return None

    client = _client_app(deny)
    with client.websocket_connect("/api/v1/voice/realtime") as ws:
        ws.send_text(json.dumps({"token": "bad"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4401


def test_reject_closes_on_session_overflow_with_4429() -> None:
    async def allow(token: str, query: dict):
        return {"user_id": "u1", "session_id": ""}

    app = FastAPI()
    register_voice_realtime_endpoints(
        app, EchoHandler(), VoiceRealtimeConfig(asr_api_key="", max_sessions=0),
        authenticator=allow,
    )
    client = TestClient(app)
    with client.websocket_connect("/api/v1/voice/realtime") as ws:
        ws.send_text(json.dumps({"token": "t"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4429


def test_ready_frame_carries_session_id() -> None:
    async def allow(token: str, query: dict):
        return {"user_id": "u1", "session_id": str(query.get("session_id") or "")}

    client = _client_app(allow)
    with client.websocket_connect("/api/v1/voice/realtime?session_id=abc123") as ws:
        ws.send_text(json.dumps({"token": "t"}))
        ready = json.loads(ws.receive_text())
    assert ready["type"] == "ready"
    assert ready["session_id"] == "abc123"


def test_superseded_kicks_old_connection_same_session() -> None:
    async def allow(token: str, query: dict):
        return {"user_id": "u1", "session_id": str(query.get("session_id") or "")}

    client = _client_app(allow)
    with client.websocket_connect("/api/v1/voice/realtime?session_id=dup1") as ws1:
        ws1.send_text(json.dumps({"token": "t"}))
        ready1 = json.loads(ws1.receive_text())
        assert ready1["type"] == "ready"
        with client.websocket_connect("/api/v1/voice/realtime?session_id=dup1") as ws2:
            ws2.send_text(json.dumps({"token": "t"}))
            ready2 = json.loads(ws2.receive_text())
            assert ready2["type"] == "ready"
            closed = json.loads(ws1.receive_text())
            assert closed["type"] == "closed"
            assert closed["reason"] == "superseded"


@pytest.mark.asyncio
async def test_superseded_marks_session_closed_and_notifies() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session.mark_superseded()
    assert session.closed is True
    assert ws.sent[-1] == {"type": "closed", "reason": "superseded"}


@pytest.mark.asyncio
async def test_heartbeat_pings_when_idle(monkeypatch) -> None:
    monkeypatch.setattr("nexus.voice_session._HEARTBEAT_S", 0.05)
    ws = FakeWebSocket()
    session = _session(ws)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.5)
    assert session.closed is False
    assert any(m["type"] == "ping" for m in ws.sent)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_half_open_send_failure_recycles_session() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    ws.fail_send = True
    task = asyncio.create_task(session.run())
    for _ in range(40):
        if session.closed:
            break
        await asyncio.sleep(0.05)
    assert session.closed is True
    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    assert ws.closed is True


@pytest.mark.asyncio
async def test_pong_is_silent_no_error_frame() -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    await session._dispatch({"type": "pong"})
    assert not any(m["type"] == "error" for m in ws.sent)


def test_heartbeat_default_is_explicit_constant() -> None:
    assert _HEARTBEAT_S == 15.0


def test_voice_usage_meter_units() -> None:
    meter = VoiceUsageMeter()
    meter.note_asr(b"x" * 32000)
    meter.note_asr(b"y" * 16000)
    meter.note_tts("你好世界")
    assert meter.asr_seconds() == 1.5
    assert meter.tts_chars == 4
    assert meter.est_asr_cny() == round(1.5 * 4.95 / 3600, 4)
    snap = meter.snapshot()
    assert snap["asr_seconds"] == 1.5 and snap["tts_chars"] == 4


@pytest.mark.asyncio
async def test_meter_report_fail_open_and_userless_skip(monkeypatch) -> None:
    import nexus.credits as credits_mod

    class Boom:
        def report_meter(self, **kwargs):
            raise RuntimeError("credits down")

    meter = VoiceUsageMeter()
    meter.note_asr(b"x" * 32000)
    monkeypatch.setattr(credits_mod, "get_credits_service", lambda: Boom())
    meter.report({"user_id": "u1"})
    meter.report({})
    reported: list[dict] = []

    class FakeSvc:
        def report_meter(self, **kwargs):
            reported.append(kwargs)

    monkeypatch.setattr(credits_mod, "get_credits_service", lambda: FakeSvc())
    meter.report({"user_id": "u9"})
    assert len(reported) == 1
    assert reported[0]["kind"] == "voice_asr"
    assert reported[0]["input_tokens"] == 1
    assert reported[0]["model"] == "volc.seedasr.sauc.duration"


@pytest.mark.asyncio
async def test_aclose_reports_connection_meter(monkeypatch) -> None:
    ws = FakeWebSocket()
    session = _session(ws)
    calls: list[dict] = []

    class FakeMeter:
        asr_bytes = 1
        tts_chars = 1

        def note_asr(self, pcm: bytes) -> None:
            pass

        def note_tts(self, text: str) -> None:
            pass

        def report(self, user: dict) -> None:
            calls.append(user)

    session._meter = FakeMeter()
    await session.aclose()
    assert calls == [{"user_id": "u1", "session_id": "s1"}]


@pytest.mark.asyncio
async def test_turn_metrics_hook_keys_and_order(monkeypatch) -> None:
    monkeypatch.setattr("nexus.voice_session.TtsLink", FakeTtsLink)
    ws = FakeWebSocket()
    handler = EchoHandler()
    session = _session(ws, handler)
    await session._on_direct_text("你好，请介绍一下你自己。")
    await session._turn_task
    assert handler.order == ["reply", "metrics", "complete"]
    metrics = handler.metrics_events[0]
    assert set(metrics.keys()) == {
        "first_audio_ms", "llm_ttft_ms", "tts_push_ms",
        "first_flush_chars", "tts_chars", "total_ms", "reply_chars",
    }
    assert metrics["first_audio_ms"] is not None
    assert metrics["tts_chars"] > 0
    assert metrics["reply_chars"] == len("你好，请自我介绍。")
    assert session._meter.tts_chars == metrics["tts_chars"]


@pytest.mark.asyncio
async def test_asr_audio_bytes_accumulate_into_meter(monkeypatch) -> None:
    class FakeAsrSession:
        def __init__(self, **kwargs) -> None:
            self.pushed: list[bytes] = []

        async def start(self) -> None:
            pass

        async def push_audio(self, pcm: bytes, last: bool = False) -> None:
            self.pushed.append(pcm)

        async def results(self):
            yield {"text": ""}

        async def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.voice_session.AsrStreamSession", FakeAsrSession)
    ws = FakeWebSocket()
    session = _session(ws, EchoHandler(), config=VoiceRealtimeConfig(asr_api_key="k"))
    import base64

    pcm = b"z" * 3200
    await session._on_audio(base64.b64encode(pcm).decode())
    await session._on_audio(base64.b64encode(pcm).decode())
    assert session._meter.asr_bytes == 6400
    assert session._meter.asr_seconds() == 0.2


@pytest.mark.asyncio
async def test_recorded_asr_stream_replay_drives_turns(monkeypatch) -> None:
    """录音重放回归（crawl-walk-run 确定性评估器）：预制 ASR 结果序列
    （partial→definite→partial→definite）经完整 run 循环重放，断言判停驱动的
    轮次触发次数与顺序——同输入必得同断言，不依赖 LLM 自评。"""

    class ReplayAsr:
        def __init__(self, **kwargs) -> None:
            pass

        async def start(self) -> None:
            pass

        async def push_audio(self, pcm: bytes, last: bool = False) -> None:
            pass

        async def results(self):
            for item in [
                {"text": "我叫张三", "definite": False, "definite_text": "", "full_text": "我叫张三"},
                {"text": "我叫张三，做了五年后端", "definite": True,
                 "definite_text": "我叫张三，做了五年后端", "full_text": "我叫张三，做了五年后端"},
                {"text": "负责过电商", "definite": False, "definite_text": "", "full_text": "负责过电商"},
                {"text": "负责过电商中台", "definite": True,
                 "definite_text": "负责过电商中台", "full_text": "我叫张三，做了五年后端。负责过电商中台"},
            ]:
                await asyncio.sleep(0.15)
                yield item
            await asyncio.sleep(3600)

        async def close(self) -> None:
            pass

    monkeypatch.setattr("nexus.voice_session.AsrStreamSession", ReplayAsr)
    import base64

    ws = FakeWebSocket()
    handler = EchoHandler()
    session = _session(ws, handler, config=VoiceRealtimeConfig(asr_api_key="k"))
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.1)
    ws.inbox.put_nowait(json.dumps({"type": "audio", "data": base64.b64encode(b"p" * 320).decode()}))
    for _ in range(60):
        if len(handler.turns) >= 2:
            break
        await asyncio.sleep(0.05)
    ws.inbox.put_nowait(json.dumps({"type": "stop"}))
    await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    assert handler.turns == ["我叫张三，做了五年后端", "负责过电商中台"]
    assert handler.metrics_events and handler.metrics_events[0]["first_audio_ms"] is None
    assert session._meter.asr_bytes == 320
