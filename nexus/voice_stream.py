"""实时语音会话流面板（r239 自 voice_session.py 拆出）：ASR 接入与 TTS 推流的 mixin 对。

拆分轴=协议适配面板 vs 会话核心：ASR 泵/音频入口/definite 判停与 TTS
预算闸/推流/出帧泵收于此处两个 mixin，会话状态机（分发/轮转/打断/收尾）
留 voice_session._RealtimeSession（mixin 继承，类身份与消费面零变化）。
patch 面随符号搬家：nexus.voice_stream.AsrStreamSession（教训 #258）。
心跳常量 _HEARTBEAT_S 与其 patch 面留守 voice_session。
"""
from __future__ import annotations

import asyncio
import base64
import time

from nexus.asr import AsrStreamError, AsrStreamSession
from nexus.logging import get_logger
from nexus.volc import TTSStreamSession

logger = get_logger("nexus.voice_session")

_MAX_AUDIO_B64 = 96 * 1024
_HARD_PUNCT = "。！？!?；;\n"
_SOFT_PUNCT = "，,、"
_ECHO_GUARD_S = 1.5
_SOFT_FLUSH_CHARS = 30
_FIRST_FLUSH_SOFT_CHARS = 12


class _RealtimeAsrMixin:
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

    async def _teardown_asr(self) -> None:
        if self._asr_pump is not None:
            self._asr_pump.cancel()
            self._asr_pump = None
        if self._asr is not None:
            await self._asr.close()
            self._asr = None


class _RealtimeTtsMixin:
    def _tts_budget_exceeded(self, text: str) -> bool:
        limit = self._config.tts_max_chars
        if limit <= 0:
            return False
        return self._turn_tts_chars + len(text) > limit

    async def _stop_tts(
        self,
        tts: TTSStreamSession,
        speaker_task: asyncio.Task | None,
    ) -> tuple[None, None]:
        if speaker_task is not None and not speaker_task.done():
            speaker_task.cancel()
        try:
            await tts.close()
        except Exception:
            pass
        await self._send({
            "type": "tts_truncated",
            "message": "本轮语音已达长度上限，后续内容以文字继续",
        })
        return None, None

    async def _push_tts(
        self,
        tts: TTSStreamSession,
        speaker_task: asyncio.Task | None,
        text: str,
    ) -> tuple[TTSStreamSession | None, asyncio.Task | None]:
        replacement, swapped = await self._tts.push_or_recover(tts, text)
        if replacement is None:
            await self._send({"type": "tts_unavailable", "message": "语音合成暂不可用，本轮以文字进行"})
            return None, None
        self._meter.note_tts(text)
        self._turn_tts_chars += len(text)
        if swapped:
            if speaker_task is not None and not speaker_task.done():
                speaker_task.cancel()
            speaker_task = asyncio.create_task(self._pump_tts(replacement))
        return replacement, speaker_task

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

    @staticmethod
    def _split_sentence(buf: str, soft_min_chars: int = _SOFT_FLUSH_CHARS) -> tuple[str, str]:
        for i, ch in enumerate(buf):
            if ch in _HARD_PUNCT:
                return buf[: i + 1], buf[i + 1:]
        for i, ch in enumerate(buf):
            if ch in _SOFT_PUNCT and i >= soft_min_chars:
                return buf[: i + 1], buf[i + 1:]
        return "", buf
