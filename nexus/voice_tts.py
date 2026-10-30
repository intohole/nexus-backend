"""TTS 连接管理：预热单连接 + 推送失败重建一次（火山双向流式单连接仅支持单次合成）。

预热在用户开始说话时触发，LLM 出文时连接已就绪，省去每轮握手延迟；
预热失败静默，正式轮取用时再正常新建。
"""
from __future__ import annotations

import asyncio

from nexus.logging import get_logger
from nexus.volc import TTSStreamSession

logger = get_logger("nexus.voice_tts")

_WARM_TIMEOUT_S = 6.0


class TtsLink:
    def __init__(self, api_key: str, voice: str, sample_rate: int) -> None:
        self._api_key = api_key
        self._voice = voice
        self._sample_rate = sample_rate
        self._warm: TTSStreamSession | None = None
        self._warm_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _new(self) -> TTSStreamSession:
        return TTSStreamSession(
            api_key=self._api_key,
            voice=self._voice,
            sample_rate=self._sample_rate,
            audio_format="pcm",
        )

    def schedule_warm(self) -> None:
        if self._warm is not None or not self._api_key:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._warm_up())

    async def _warm_up(self) -> None:
        tts = self._new()
        try:
            await asyncio.wait_for(tts.start(), timeout=_WARM_TIMEOUT_S)
            if self._warm is not None:
                await tts.close()
                return
            self._warm = tts
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("tts prewarm failed: %s", e)
            try:
                await tts.close()
            except Exception:
                pass

    async def acquire(self) -> TTSStreamSession | None:
        warm = self._warm
        self._warm = None
        if warm is not None:
            return warm
        if not self._api_key:
            return None
        tts = self._new()
        try:
            await tts.start()
            return tts
        except Exception as e:
            logger.warning("tts start failed, degrade to text-only turn: %s", e)
            try:
                await tts.close()
            except Exception:
                pass
            return None

    async def push_or_recover(self, tts: TTSStreamSession, text: str) -> tuple[TTSStreamSession | None, bool]:
        """推文本；失败关旧连接重建一次。返回 (连接|None, 是否换了新连接)。"""
        try:
            await tts.push_text(text)
            return tts, False
        except Exception as e:
            logger.warning("tts push failed: %s", e)
            try:
                await tts.close()
            except Exception:
                pass
            replacement = self._new()
            try:
                await replacement.start()
                await replacement.push_text(text)
                return replacement, True
            except Exception as e2:
                logger.warning("tts recover failed: %s", e2)
                try:
                    await replacement.close()
                except Exception:
                    pass
                return None, False

    async def close_warm(self) -> None:
        if self._warm_task is not None:
            self._warm_task.cancel()
            self._warm_task = None
        if self._warm is not None:
            try:
                await self._warm.close()
            except Exception:
                pass
            self._warm = None
