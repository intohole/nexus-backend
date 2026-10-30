"""语音模态用量计量：连接级 ASR 字节 / TTS 字符累计，会话收口批量上报 usage_meters。

火山计价口径：ASR(volc.seedasr.sauc.duration) 按时长、TTS 按字符。
observe 态只记量不拦调用；上报走 MeteringBuffer（同步 append，fail-open，
计量失败绝不阻断语音链——账本与动作解耦）。
"""
from __future__ import annotations

import time
from typing import Optional

from nexus.logging import get_logger

logger = get_logger("nexus.voice_meter")

ASR_BYTES_PER_SECOND = 32000
ASR_EST_CNY_PER_HOUR = 4.95


class VoiceUsageMeter:
    def __init__(self) -> None:
        self.asr_bytes = 0
        self.tts_chars = 0
        self.started_at = time.monotonic()

    def note_asr(self, pcm: bytes) -> None:
        self.asr_bytes += len(pcm)

    def note_tts(self, text: str) -> None:
        self.tts_chars += len(text)

    def asr_seconds(self) -> float:
        return round(self.asr_bytes / ASR_BYTES_PER_SECOND, 3)

    def est_asr_cny(self) -> float:
        return round(self.asr_seconds() * ASR_EST_CNY_PER_HOUR / 3600, 4)

    def snapshot(self) -> dict[str, object]:
        return {"asr_seconds": self.asr_seconds(), "tts_chars": self.tts_chars,
                "est_asr_cny": self.est_asr_cny()}

    def report(self, user: dict) -> None:
        user_id = str((user or {}).get("user_id") or "")
        if not user_id or (not self.asr_bytes and not self.tts_chars):
            return
        try:
            from nexus.credits import get_credits_service

            svc = get_credits_service()
            if self.asr_bytes:
                svc.report_meter(
                    kind="voice_asr", feature="asr_seconds", model="volc.seedasr.sauc.duration",
                    input_tokens=int(self.asr_seconds()),
                    duration_ms=int(self.asr_seconds() * 1000), user_id=user_id,
                )
            if self.tts_chars:
                svc.report_meter(
                    kind="voice_tts", feature="tts_chars", model="volc.tts.bidirection",
                    input_tokens=self.tts_chars, user_id=user_id,
                )
        except Exception as e:
            logger.warning("voice meter report failed (fail-open): %s", e)


def collect_turn_metrics(session: object, reply_text: str) -> dict[str, object]:
    """从会话的 turn 级观测字段汇总一轮延迟指标（total_ms 现算，其余取现值）。"""
    started = getattr(session, "_turn_started_at", 0.0)
    total_ms = int((time.monotonic() - started) * 1000) if started else None

    def _opt(name: str) -> Optional[int]:
        value = getattr(session, name, None)
        return int(value) if value is not None else None

    return {
        "first_audio_ms": _opt("_first_audio_ms"),
        "llm_ttft_ms": _opt("_llm_ttft_ms"),
        "tts_push_ms": _opt("_tts_first_push_ms"),
        "first_flush_chars": _opt("_first_flush_chars"),
        "tts_chars": int(getattr(session, "_turn_tts_chars", 0) or 0),
        "total_ms": total_ms,
        "reply_chars": len(reply_text),
    }
