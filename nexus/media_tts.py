"""TTS 合成到 MP3：标点切分 + 内容寻址缓存 + pcm→mp3 转码单源（火山语音）。

消费方按「内容寻址缓存命中即返回，未命中走合成」使用；imageio-ffmpeg
由消费方仓声明依赖，此处惰性 import，缺包时在转码点 fail-loud。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from nexus.logging import get_logger
from nexus.volc import TTSStreamSession

logger = get_logger("nexus.media_tts")

_SAMPLE_RATE = 24000
_DEFAULT_MAX_SEGMENT = 1800
_SPLIT_MARKS = ("。", "！", "？", "；", "\n")
_FFMPEG_TIMEOUT_S = 120


class TTSUnavailableError(RuntimeError):
    pass


def split_text(text: str, limit: int = _DEFAULT_MAX_SEGMENT) -> list[str]:
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) >= limit and ch in _SPLIT_MARKS:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return parts


def cache_digest(salt: str, voice: str, text: str) -> str:
    return hashlib.md5(f"{salt}:{voice}:{text}".encode()).hexdigest()


async def resolve_volc_tts_key() -> str:
    key = os.getenv("VOLC_TTS_API_KEY", "")
    if key:
        return key
    from nexus.infra import get_volc_speech_config

    config = await get_volc_speech_config()
    return str(config.get("tts_api_key") or "")


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


async def pcm_to_mp3(pcm_path: Path, mp3_path: Path, sample_rate: int = _SAMPLE_RATE) -> None:
    if pcm_path.stat().st_size < 2:
        raise TTSUnavailableError("pcm 输入为空，拒绝转码出空 mp3 缓存")
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        _ffmpeg_exe(), "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", str(pcm_path), "-codec:a", "libmp3lame", "-q:a", "4", str(mp3_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.wait(), timeout=_FFMPEG_TIMEOUT_S)
    if proc.returncode != 0 or not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise TTSUnavailableError("pcm 转 mp3 失败")


async def _synthesize_pcm(
    api_key: str, voice: str, text: str, *, uid: str, sample_rate: int, max_segment_chars: int
) -> bytes:
    session = TTSStreamSession(
        api_key=api_key, voice=voice, sample_rate=sample_rate,
        audio_format="pcm", uid=uid,
    )
    await session.start()
    try:
        for seg in split_text(text, max_segment_chars):
            await session.push_text(seg)
        await session.finish()
        chunks: list[bytes] = []
        async for chunk in session.frames():
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        try:
            await session.close()
        except Exception:
            logger.debug("tts session close suppressed", exc_info=True)


async def synthesize_mp3(
    text: str,
    voice: str,
    cache_dir: Path | str,
    salt: str,
    *,
    uid: str = "nexus-tts",
    sample_rate: int = _SAMPLE_RATE,
    max_segment_chars: int = _DEFAULT_MAX_SEGMENT,
) -> Path:
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    digest = cache_digest(salt, voice, text)
    final_path = cache / f"{digest}.mp3"
    if final_path.exists() and final_path.stat().st_size:
        return final_path
    api_key = await resolve_volc_tts_key()
    if not api_key:
        raise TTSUnavailableError(
            "语音密钥未配置（Lion infra.volc_speech.tts_api_key 或 env VOLC_TTS_API_KEY）"
        )
    pcm = await _synthesize_pcm(
        api_key, voice, text, uid=uid, sample_rate=sample_rate, max_segment_chars=max_segment_chars
    )
    if not pcm:
        raise TTSUnavailableError("语音合成返回空音频")
    raw_path = cache / f"{digest}.pcm"
    raw_path.write_bytes(pcm)
    try:
        await pcm_to_mp3(raw_path, final_path, sample_rate=sample_rate)
    finally:
        raw_path.unlink(missing_ok=True)
    return final_path
