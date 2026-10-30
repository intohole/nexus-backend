"""实时语音会话编排：流式ASR → 业务回复流 → 流式TTS 三路流的单连接生命周期。

判停由 ASR definite 驱动（客户端 VAD turn_end 仅兜底），ASR 会话跨轮复用，
TTS 连接按轮预热（火山双向流式单连接仅支持单次合成），打断需客户端信号门控。
TTFA 分段观测（llm_ttft/tts_push/first_flush）；首段 flush 软标点阈值提前到
12 字加速首包（后续段 30 字保韵律，flush 段恒以标点结尾避开 TTS 等待）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from fastapi import WebSocket, WebSocketDisconnect

from nexus.asr import AsrStreamError, AsrStreamSession
from nexus.logging import get_logger
from nexus.voice_meter import VoiceUsageMeter, collect_turn_metrics
from nexus.voice_tts import TtsLink
from nexus.volc import TTSStreamSession, VolcArkError

logger = get_logger("nexus.voice_session")

_MAX_AUDIO_B64 = 96 * 1024
_HARD_PUNCT = "。！？!?；;\n"
_SOFT_PUNCT = "，,、"
_DEFINITE_WAIT_S = 1.2
_ECHO_GUARD_S = 1.5
_SOFT_FLUSH_CHARS = 30
_FIRST_FLUSH_SOFT_CHARS = 12
_HEARTBEAT_S = 15.0


class VoiceTurnHandler(Protocol):
    async def reply(self, user_text: str, ctx: dict) -> AsyncIterator[str]: ...


@dataclass
class VoiceRealtimeConfig:
    tts_api_key: str = ""
    tts_voice: str = "zh_female_gaolengyujie_uranus_bigtts"
    tts_sample_rate: int = 16000
    asr_api_key: str = ""
    asr_resource_id: str = "volc.seedasr.sauc.duration"
    asr_end_window_ms: int = 500
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
        self._turn_lock = asyncio.Lock()
        self._tts = TtsLink(config.tts_api_key, config.tts_voice, config.tts_sample_rate)
        self._pending_text = ""
        self._last_partial = ""
        self._last_activity = time.monotonic()
        self._last_client_interrupt = 0.0
        self._closed = False
        self._cleaned = False
        self._started_at = time.monotonic()
        self._turn_user_text: str | None = None
        self._turn_reply_parts: list[str] | None = None
        self._turn_started_at = 0.0
        self._first_audio_ms: int | None = None
        self._llm_ttft_ms: int | None = None
        self._tts_first_push_ms: int | None = None
        self._first_flush_chars: int | None = None
        self._last_tts_audio_mono = 0.0
        self._ctx: dict = {"conn_id": self._conn_id, "user": user}
        self._meter = VoiceUsageMeter()
        self._last_send_mono = time.monotonic()
        self._turn_tts_chars = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self) -> None:
        await self._send({
            "type": "ready", "conn_id": self._conn_id,
            "user_id": self._user.get("user_id"),
            "session_id": str(self._user.get("session_id") or ""),
        })
        try:
            while not self._closed:
                if time.monotonic() - self._started_at > self._config.session_timeout_s:
                    await self._send({"type": "closed", "reason": "session timeout"})
                    break
                try:
                    raw = await asyncio.wait_for(self._ws.receive_text(), timeout=0.2)
                except asyncio.TimeoutError:
                    await self._idle_check()
                    await self._heartbeat()
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
            await self._trigger_turn(source="client")
        elif msg_type == "interrupt":
            self._last_client_interrupt = time.monotonic()
            await self._interrupt(notify=True)
        elif msg_type == "pong":
            pass
        elif msg_type == "stop":
            self._closed = True
        else:
            await self._send({"type": "error", "message": f"未知消息类型 {msg_type}"})

    async def _heartbeat(self) -> None:
        if time.monotonic() - self._last_send_mono >= _HEARTBEAT_S:
            await self._send({"type": "ping"})

    async def mark_superseded(self) -> None:
        self._closed = True
        await self._send({"type": "closed", "reason": "superseded"})

    async def _on_audio(self, b64: str) -> None:
        if not b64 or len(b64) > _MAX_AUDIO_B64:
            return
        if self._asr is None:
            await self._open_asr()
        if self._asr is None:
            return
        pcm = base64.b64decode(b64)
        try:
            await self._asr.push_audio(pcm)
            self._meter.note_asr(pcm)
        except (AsrStreamError, Exception) as e:
            logger.warning("asr push failed: %s", e)
            await self._teardown_asr()

    async def _on_direct_text(self, text: str) -> None:
        text = text.strip()
        if text:
            self._pending_text = (self._pending_text + text)[: self._config.max_user_chars]
        await self._trigger_turn(source="text")

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
        self._tts.schedule_warm()

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
                    await self._on_definite(str(r.get("definite_text") or ""), r.get("full_text"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("asr pump exit: %s", e)
            await self._teardown_asr()

    async def _on_definite(self, definite: str, full_text: object) -> None:
        self._pending_text = (self._pending_text + definite)[: self._config.max_user_chars]
        await self._send({"type": "asr_final", "text": definite, "full": full_text})
        barge_in_window = time.monotonic() - self._last_client_interrupt <= 3.0
        if (
            self._config.allow_barge_in
            and barge_in_window
            and self._turn_task is not None
            and not self._turn_task.done()
        ):
            await self._interrupt(notify=True)
        if time.monotonic() - self._last_tts_audio_mono < _ECHO_GUARD_S:
            return
        await self._trigger_turn(source="definite")

    async def _idle_check(self) -> None:
        if self._pending_text and (self._turn_task is None or self._turn_task.done()):
            if time.monotonic() - self._last_activity >= self._config.turn_idle_timeout_s:
                await self._trigger_turn(source="idle")

    async def _trigger_turn(self, source: str) -> None:
        if self._closed:
            return
        if self._asr is not None and source != "definite" and self._pending_text and self._turn_slot_free():
            base_len = len(self._pending_text)
            deadline = time.monotonic() + _DEFINITE_WAIT_S
            while time.monotonic() < deadline and len(self._pending_text) <= base_len and self._turn_slot_free():
                await asyncio.sleep(0.1)
        async with self._turn_lock:
            if not self._turn_slot_free():
                return
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
        self._first_audio_ms = None
        self._llm_ttft_ms = None
        self._tts_first_push_ms = None
        self._first_flush_chars = None
        self._turn_tts_chars = 0
        self._turn_started_at = time.monotonic()
        await self._send({"type": "thinking", "user": user_text})
        tts = await self._tts.acquire()
        if tts is None and self._tts.enabled:
            await self._send({"type": "tts_unavailable", "message": "语音合成暂不可用，本轮以文字进行"})
        speaker_task: asyncio.Task | None = asyncio.create_task(self._pump_tts(tts)) if tts is not None else None
        reply_parts: list[str] = []
        self._turn_reply_parts = reply_parts
        try:
            sentence_buf = ""
            first_flush_done = False
            async for delta in self._handler.reply(user_text, dict(self._ctx)):
                if not delta:
                    continue
                if self._llm_ttft_ms is None:
                    self._llm_ttft_ms = int((time.monotonic() - self._turn_started_at) * 1000)
                reply_parts.append(delta)
                await self._send({"type": "assistant_delta", "text": delta})
                if tts is not None:
                    sentence_buf += delta
                    soft_min = _SOFT_FLUSH_CHARS if first_flush_done else _FIRST_FLUSH_SOFT_CHARS
                    flush, sentence_buf = self._split_sentence(sentence_buf, soft_min)
                    if flush:
                        first_flush_done = True
                        if self._first_flush_chars is None:
                            self._first_flush_chars = len(flush)
                        if self._tts_first_push_ms is None:
                            self._tts_first_push_ms = int((time.monotonic() - self._turn_started_at) * 1000)
                        tts, speaker_task = await self._push_tts(tts, speaker_task, flush)
                        if tts is None:
                            break
            if tts is not None:
                if sentence_buf.strip():
                    tts, speaker_task = await self._push_tts(tts, speaker_task, sentence_buf)
                if tts is not None:
                    await tts.finish()
                    try:
                        await asyncio.wait_for(asyncio.shield(speaker_task), timeout=self._config.turn_timeout_s)
                    except asyncio.TimeoutError:
                        logger.warning("tts drain timeout turn conn=%s", self._conn_id)
            reply_text = "".join(reply_parts)
            await self._complete_turn(user_text, reply_text)
        except asyncio.CancelledError:
            raise
        except (VolcArkError, AsrStreamError, Exception) as e:
            logger.warning("turn failed conn=%s: %s", self._conn_id, e)
            reply_text = "".join(reply_parts)
            await self._send({"type": "error", "message": "本轮处理失败，请重试"})
            await self._send({
                "type": "turn_end", "user": user_text, "reply": reply_text,
                "failed": True, "first_audio_ms": self._first_audio_ms,
            })
            await self._notify_turn_interrupted(user_text, reply_text)
            self._turn_user_text = None
            self._turn_reply_parts = None
        finally:
            if speaker_task is not None and not speaker_task.done():
                speaker_task.cancel()
            if tts is not None:
                await tts.close()
            if not self._closed:
                self._tts.schedule_warm()

    async def _push_tts(
        self,
        tts: TTSStreamSession,
        speaker_task: asyncio.Task | None,
        text: str,
    ) -> tuple[TTSStreamSession | None, asyncio.Task | None]:
        replacement, swapped = await self._tts.push_or_recover(tts, text)
        self._meter.note_tts(text)
        self._turn_tts_chars += len(text)
        if swapped:
            if speaker_task is not None and not speaker_task.done():
                speaker_task.cancel()
            speaker_task = asyncio.create_task(self._pump_tts(replacement))
        if replacement is None:
            await self._send({"type": "tts_unavailable", "message": "语音合成暂不可用，本轮以文字进行"})
            return None, None
        return replacement, speaker_task

    async def _complete_turn(self, user_text: str, reply_text: str) -> None:
        metrics = collect_turn_metrics(self, reply_text)
        on_metrics = getattr(self._handler, "on_turn_metrics", None)
        if on_metrics is not None:
            try:
                await on_metrics(metrics, dict(self._ctx))
            except Exception as e:
                logger.warning("handler on_turn_metrics failed: %s", e)
        on_turn = getattr(self._handler, "on_turn_complete", None)
        if on_turn is not None:
            try:
                await on_turn(user_text, reply_text, dict(self._ctx))
            except Exception as e:
                logger.warning("handler on_turn_complete failed: %s", e)
        await self._send({
            "type": "turn_end", "user": user_text, "reply": reply_text,
            "first_audio_ms": self._first_audio_ms, "llm_ttft_ms": self._llm_ttft_ms,
        })
        self._log_latency(reply_text)
        self._turn_user_text = None
        self._turn_reply_parts = None

    async def _pump_tts(self, tts: TTSStreamSession) -> None:
        try:
            async for chunk in tts.frames():
                if self._closed:
                    return
                if self._first_audio_ms is None:
                    self._first_audio_ms = int((time.monotonic() - self._turn_started_at) * 1000)
                self._last_tts_audio_mono = time.monotonic()
                await self._send({"type": "tts_audio", "data": base64.b64encode(chunk).decode()})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("tts pump exit: %s", e)

    def _log_latency(self, reply_text: str) -> None:
        if self._first_audio_ms is None:
            return
        total_ms = int((time.monotonic() - self._turn_started_at) * 1000)
        logger.info(
            "voice turn latency conn=%s first_audio_ms=%s llm_ttft_ms=%s tts_push_ms=%s "
            "first_flush_chars=%s total_ms=%s reply_chars=%s",
            self._conn_id, self._first_audio_ms, self._llm_ttft_ms, self._tts_first_push_ms,
            self._first_flush_chars, total_ms, len(reply_text),
        )

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
    def _split_sentence(buf: str, soft_min_chars: int = _SOFT_FLUSH_CHARS) -> tuple[str, str]:
        for i, ch in enumerate(buf):
            if ch in _HARD_PUNCT:
                return buf[: i + 1], buf[i + 1:]
        for i, ch in enumerate(buf):
            if ch in _SOFT_PUNCT and i >= soft_min_chars:
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
            self._last_send_mono = time.monotonic()
        except Exception:
            self._closed = True

    async def aclose(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._closed = True
        await self._interrupt(notify=False)
        await self._tts.close_warm()
        await self._teardown_asr()
        self._meter.report(self._user)
        try:
            await self._ws.close()
        except Exception:
            pass
