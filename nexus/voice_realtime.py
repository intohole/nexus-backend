"""实时语音对话 WS 网关: register_voice_realtime_endpoints(app, handler, config)。

编排 流式ASR → 业务回复流(VoiceTurnHandler.reply) → 流式TTS 三路流，
支持增量字幕、打断(barge-in)、text 输入降级、会话并发与超时治理。
一行注册即接入，业务侧只需实现 VoiceTurnHandler。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from nexus.asr import AsrStreamError, AsrStreamSession
from nexus.logging import get_logger
from nexus.volc import TTSStreamSession, VolcArkError

logger = get_logger("nexus.voice_realtime")

_MAX_AUDIO_B64 = 96 * 1024
_HARD_PUNCT = "。！？!?；;\n"
_SOFT_PUNCT = "，,、"
_RECV_POLL_S = 0.2
_AUTH_TIMEOUT_S = 5.0


class VoiceTurnHandler(Protocol):
    async def reply(self, user_text: str, ctx: dict) -> AsyncIterator[str]: ...


@dataclass
class VoiceRealtimeConfig:
    tts_api_key: str = ""
    tts_voice: str = "zh_female_gaolengyujie_uranus_bigtts"
    tts_sample_rate: int = 16000
    asr_api_key: str = ""
    asr_resource_id: str = "volc.seedasr.sauc.duration"
    asr_end_window_ms: int = 800
    asr_enable_nonstream: bool = True
    allow_barge_in: bool = True
    max_sessions: int = 32
    session_timeout_s: float = 900.0
    turn_idle_timeout_s: float = 2.5
    turn_timeout_s: float = 180.0
    max_user_chars: int = 2000
    min_turn_chars: int = 2
    uid_prefix: str = "nexus-vr"


class _RealtimeSession:
    def __init__(
        self,
        ws: WebSocket,
        handler: VoiceTurnHandler,
        config: VoiceRealtimeConfig,
        user: dict,
    ) -> None:
        self._ws = ws
        self._handler = handler
        self._config = config
        self._user = user
        self._conn_id = uuid.uuid4().hex[:12]
        self._asr: AsrStreamSession | None = None
        self._asr_pump: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._pending_text = ""
        self._last_partial = ""
        self._last_activity = time.monotonic()
        self._last_client_interrupt = 0.0
        self._closed = False
        self._cleaned = False
        self._started_at = time.monotonic()
        self._turn_user_text: str | None = None
        self._turn_reply_parts: list[str] | None = None
        self._ctx: dict = {"conn_id": self._conn_id, "user": user}

    async def run(self) -> None:
        await self._send({"type": "ready", "conn_id": self._conn_id, "user_id": self._user.get("user_id")})
        try:
            while not self._closed:
                if time.monotonic() - self._started_at > self._config.session_timeout_s:
                    await self._send({"type": "closed", "reason": "session timeout"})
                    break
                try:
                    raw = await asyncio.wait_for(self._ws.receive_text(), timeout=_RECV_POLL_S)
                except asyncio.TimeoutError:
                    await self._idle_check()
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    await self._send({"type": "error", "message": "消息格式错误"})
                    continue
                await self._dispatch(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("voice ws session %s exit: %s", self._conn_id, e)
        finally:
            await self.aclose()

    async def _dispatch(self, msg: dict) -> None:
        self._last_activity = time.monotonic()
        msg_type = str(msg.get("type") or "")
        if msg_type == "audio":
            await self._on_audio(str(msg.get("data") or ""))
        elif msg_type == "text":
            await self._on_direct_text(str(msg.get("text") or ""))
        elif msg_type == "turn_end":
            await self._trigger_turn()
        elif msg_type == "interrupt":
            self._last_client_interrupt = time.monotonic()
            await self._interrupt(notify=True)
        elif msg_type == "stop":
            self._closed = True
        else:
            await self._send({"type": "error", "message": f"未知消息类型 {msg_type}"})

    async def _on_audio(self, b64: str) -> None:
        if not b64 or len(b64) > _MAX_AUDIO_B64:
            return
        if self._asr is None:
            await self._open_asr()
        if self._asr is None:
            return
        try:
            await self._asr.push_audio(base64.b64decode(b64))
        except (AsrStreamError, Exception) as e:
            logger.warning("asr push failed: %s", e)
            await self._teardown_asr()

    async def _on_direct_text(self, text: str) -> None:
        text = text.strip()
        if text:
            self._pending_text = (self._pending_text + text)[: self._config.max_user_chars]
        await self._trigger_turn()

    async def _open_asr(self) -> None:
        if not self._config.asr_api_key:
            await self._send({"type": "error", "message": "语音识别服务未配置，可用文本模式（发送 type:text）"})
            return
        session = AsrStreamSession(
            api_key=self._config.asr_api_key,
            resource_id=self._config.asr_resource_id,
            end_window_ms=self._config.asr_end_window_ms,
            enable_nonstream=self._config.asr_enable_nonstream,
            uid=f"{self._config.uid_prefix}-{self._conn_id}",
        )
        try:
            await session.start()
        except (AsrStreamError, Exception) as e:
            logger.warning("asr open failed: %s", e)
            await self._send({"type": "error", "message": "语音识别连接失败，请稍后重试或用文本模式"})
            return
        self._asr = session
        self._asr_pump = asyncio.create_task(self._asr_results_pump(session))

    async def _asr_results_pump(self, session: AsrStreamSession) -> None:
        try:
            async for r in session.results():
                if self._closed:
                    return
                if "error" in r:
                    await self._send({"type": "error", "message": f"语音识别异常: {r['error']}"})
                    await self._teardown_asr()
                    return
                text = str(r.get("text") or "")
                if text:
                    self._last_partial = text
                    await self._send({"type": "asr_partial", "text": text})
                if r.get("definite"):
                    definite = str(r.get("definite_text") or "")
                    self._pending_text = (self._pending_text + definite)[: self._config.max_user_chars]
                    await self._send({"type": "asr_final", "text": definite, "full": r.get("full_text")})
                    barge_in_window = time.monotonic() - self._last_client_interrupt <= 3.0
                    if (
                        self._config.allow_barge_in
                        and barge_in_window
                        and self._turn_task is not None
                        and not self._turn_task.done()
                    ):
                        await self._interrupt(notify=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("asr pump exit: %s", e)
            await self._teardown_asr()

    async def _idle_check(self) -> None:
        if self._pending_text and (self._turn_task is None or self._turn_task.done()):
            if time.monotonic() - self._last_activity >= self._config.turn_idle_timeout_s:
                await self._trigger_turn()

    async def _trigger_turn(self) -> None:
        if self._closed or not self._turn_slot_free():
            return
        base_len = len(self._pending_text)
        if self._asr is not None:
            try:
                await self._asr.finish()
            except Exception:
                pass
            deadline = time.monotonic() + 1.6
            while time.monotonic() < deadline and len(self._pending_text) <= base_len:
                await asyncio.sleep(0.1)
        user_text = (self._pending_text.strip() or self._last_partial.strip())[: self._config.max_user_chars]
        if len(user_text) < self._config.min_turn_chars:
            return
        self._pending_text = ""
        self._last_partial = ""
        self._turn_task = asyncio.create_task(self._run_turn(user_text))

    def _turn_slot_free(self) -> bool:
        return self._turn_task is None or self._turn_task.done()

    async def _run_turn(self, user_text: str) -> None:
        self._turn_user_text = user_text
        await self._send({"type": "thinking", "user": user_text})
        tts: TTSStreamSession | None = None
        if self._config.tts_api_key:
            tts = TTSStreamSession(
                api_key=self._config.tts_api_key,
                voice=self._config.tts_voice,
                sample_rate=self._config.tts_sample_rate,
                audio_format="pcm",
            )
            try:
                await tts.start()
            except Exception as e:
                logger.warning("tts start failed, degrade to text-only turn conn=%s: %s", self._conn_id, e)
                await self._send({"type": "tts_unavailable", "message": "语音合成暂不可用，本轮以文字进行"})
                try:
                    await tts.close()
                except Exception:
                    pass
                tts = None
        speaker_task: asyncio.Task | None = None
        reply_parts: list[str] = []
        self._turn_reply_parts = reply_parts
        try:
            if tts is not None:
                speaker_task = asyncio.create_task(self._pump_tts(tts))
            sentence_buf = ""
            async for delta in self._handler.reply(user_text, dict(self._ctx)):
                if not delta:
                    continue
                reply_parts.append(delta)
                await self._send({"type": "assistant_delta", "text": delta})
                if tts is not None:
                    sentence_buf += delta
                    flush, sentence_buf = self._split_sentence(sentence_buf)
                    if flush:
                        try:
                            await tts.push_text(flush)
                        except Exception as e:
                            logger.warning("tts push failed, degrade to text-only conn=%s: %s", self._conn_id, e)
                            await self._send({"type": "tts_unavailable", "message": "语音合成暂不可用，本轮以文字进行"})
                            tts = None
            if tts is not None:
                if sentence_buf.strip():
                    try:
                        await tts.push_text(sentence_buf)
                    except Exception:
                        pass
                await tts.finish()
                try:
                    await asyncio.wait_for(asyncio.shield(speaker_task), timeout=self._config.turn_timeout_s)
                except asyncio.TimeoutError:
                    logger.warning("tts drain timeout turn conn=%s", self._conn_id)
            reply_text = "".join(reply_parts)
            on_turn = getattr(self._handler, "on_turn_complete", None)
            if on_turn is not None:
                try:
                    await on_turn(user_text, reply_text, dict(self._ctx))
                except Exception as e:
                    logger.warning("handler on_turn_complete failed: %s", e)
            await self._send({"type": "turn_end", "user": user_text, "reply": reply_text})
            self._turn_user_text = None
            self._turn_reply_parts = None
        except asyncio.CancelledError:
            raise
        except (VolcArkError, AsrStreamError, Exception) as e:
            logger.warning("turn failed conn=%s: %s", self._conn_id, e)
            await self._send({"type": "error", "message": "本轮处理失败，请重试"})
            await self._send({
                "type": "turn_end",
                "user": user_text,
                "reply": "".join(reply_parts),
                "failed": True,
            })
            await self._notify_turn_interrupted(user_text, "".join(reply_parts))
            self._turn_user_text = None
            self._turn_reply_parts = None
        finally:
            if speaker_task is not None and not speaker_task.done():
                speaker_task.cancel()
            if tts is not None:
                await tts.close()
            await self._teardown_asr()

    async def _pump_tts(self, tts: TTSStreamSession) -> None:
        try:
            async for chunk in tts.frames():
                if self._closed:
                    return
                await self._send({"type": "tts_audio", "data": base64.b64encode(chunk).decode()})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("tts pump exit: %s", e)

    async def _interrupt(self, notify: bool) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
            try:
                await self._turn_task
            except (asyncio.CancelledError, Exception):
                pass
            user_text = self._turn_user_text or ""
            partial = "".join(self._turn_reply_parts or [])
            self._turn_user_text = None
            self._turn_reply_parts = None
            if notify:
                await self._send({"type": "interrupted"})
            await self._notify_turn_interrupted(user_text, partial)
        self._turn_task = None

    async def _notify_turn_interrupted(self, user_text: str, partial_reply: str) -> None:
        hook = getattr(self._handler, "on_turn_interrupted", None)
        if hook is None or not user_text or not partial_reply.strip():
            return
        try:
            await hook(user_text, partial_reply, dict(self._ctx))
        except Exception as e:
            logger.warning("handler on_turn_interrupted failed: %s", e)

    @staticmethod
    def _split_sentence(buf: str) -> tuple[str, str]:
        for i, ch in enumerate(buf):
            if ch in _HARD_PUNCT:
                return buf[: i + 1], buf[i + 1:]
        for i, ch in enumerate(buf):
            if ch in _SOFT_PUNCT and i >= 30:
                return buf[: i + 1], buf[i + 1:]
        return "", buf

    async def _teardown_asr(self) -> None:
        if self._asr_pump is not None:
            self._asr_pump.cancel()
            self._asr_pump = None
        if self._asr is not None:
            await self._asr.close()
            self._asr = None

    async def _send(self, payload: dict) -> None:
        try:
            await self._ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception:
            self._closed = True

    async def aclose(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._closed = True
        await self._interrupt(notify=False)
        await self._teardown_asr()
        try:
            await self._ws.close()
        except Exception:
            pass


_active_sessions: set[_RealtimeSession] = set()


async def _default_authenticator(token: str, query: dict) -> dict:
    return {"user_id": "anonymous"} if token else {}


def register_voice_realtime_endpoints(
    app: FastAPI,
    handler: VoiceTurnHandler,
    config: VoiceRealtimeConfig,
    authenticator: Callable[[str, dict], Awaitable[dict | None]] | None = None,
    path: str = "/api/v1/voice/realtime",
) -> None:
    """注册 WS {path}。鉴权走首帧 hello {type:"auth",token}（5s 超时），
    authenticator(token, query)->user|None，None 拒绝(4401)；token 不进 URL 避免访问日志泄漏。"""
    auth = authenticator or _default_authenticator

    @app.websocket(path)
    async def voice_realtime_ws(websocket: WebSocket) -> None:
        query = dict(websocket.query_params)
        await websocket.accept()
        try:
            hello_raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect, Exception):
            await _reject(websocket, 4401, "auth timeout")
            return
        token = ""
        try:
            token = str(json.loads(hello_raw).get("token") or "")
        except ValueError:
            pass
        try:
            user = await auth(token, query)
        except Exception as e:
            logger.warning("voice ws auth error: %s", e)
            user = None
        if not user:
            await _reject(websocket, 4401, "unauthorized")
            return
        if len(_active_sessions) >= config.max_sessions:
            await _reject(websocket, 4429, "too many sessions")
            return
        session = _RealtimeSession(websocket, handler, config, dict(user))
        _active_sessions.add(session)
        try:
            await session.run()
        finally:
            _active_sessions.discard(session)


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.accept()
        await websocket.close(code=code, reason=reason)
    except Exception:
        pass
