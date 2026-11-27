from __future__ import annotations

import asyncio
import struct

import pytest

import nexus.media_tts as mt
from nexus.media_tts import (
    TTSUnavailableError,
    cache_digest,
    pcm_to_mp3,
    resolve_volc_tts_key,
    split_text,
    synthesize_mp3,
)

_PCM_SILENCE = struct.pack("<h", 0) * 2400


def test_split_text_marks_and_tail():
    parts = split_text("第一句。第二句！第三句", limit=5)
    assert parts[0] == "第一句。第二句！"
    assert parts[-1] == "第三句"
    assert split_text("ab", limit=1800) == ["ab"]


def test_cache_digest_stable():
    assert cache_digest("s", "v", "t") == cache_digest("s", "v", "t")
    assert cache_digest("s", "v", "t") != cache_digest("s", "v2", "t")


def test_resolve_key_env_first(monkeypatch):
    monkeypatch.setenv("VOLC_TTS_API_KEY", "env-key")
    assert asyncio.run(resolve_volc_tts_key()) == "env-key"


def test_resolve_key_config_fallback(monkeypatch):
    monkeypatch.delenv("VOLC_TTS_API_KEY", raising=False)

    async def fake_config():
        return {"tts_api_key": "cfg-key"}

    monkeypatch.setattr("nexus.infra.get_volc_speech_config", fake_config)
    assert asyncio.run(resolve_volc_tts_key()) == "cfg-key"


def test_resolve_key_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("VOLC_TTS_API_KEY", raising=False)

    async def fake_config():
        return {}

    monkeypatch.setattr("nexus.infra.get_volc_speech_config", fake_config)
    assert asyncio.run(resolve_volc_tts_key()) == ""


def test_synthesize_cache_hit_skips_synth(tmp_path, monkeypatch):
    import hashlib

    digest = hashlib.md5("salt:voice:hi".encode()).hexdigest()
    (tmp_path / f"{digest}.mp3").write_bytes(b"mp3-bytes")
    calls: list[int] = []

    async def fake_pcm(*a, **k):
        calls.append(1)
        return b"pcm"

    monkeypatch.setattr(mt, "_synthesize_pcm", fake_pcm)
    out = asyncio.run(synthesize_mp3("hi", "voice", tmp_path, "salt"))
    assert out.read_bytes() == b"mp3-bytes" and calls == []


def test_synthesize_no_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VOLC_TTS_API_KEY", raising=False)

    async def fake_config():
        return {}

    monkeypatch.setattr("nexus.infra.get_volc_speech_config", fake_config)
    with pytest.raises(TTSUnavailableError):
        asyncio.run(synthesize_mp3("hi", "voice", tmp_path, "salt"))


def test_synthesize_empty_pcm_raises(tmp_path, monkeypatch):
    async def fake_key():
        return "k"

    async def fake_pcm(*a, **k):
        return b""

    monkeypatch.setattr(mt, "resolve_volc_tts_key", fake_key)
    monkeypatch.setattr(mt, "_synthesize_pcm", fake_pcm)
    with pytest.raises(TTSUnavailableError):
        asyncio.run(synthesize_mp3("hi", "voice", tmp_path, "salt"))


def test_synthesize_full_pipeline_real_ffmpeg(tmp_path, monkeypatch):
    async def fake_key():
        return "k"

    async def fake_pcm(*a, **k):
        return _PCM_SILENCE

    monkeypatch.setattr(mt, "resolve_volc_tts_key", fake_key)
    monkeypatch.setattr(mt, "_synthesize_pcm", fake_pcm)
    out = asyncio.run(synthesize_mp3("hi", "voice", tmp_path, "salt"))
    assert out.exists() and out.stat().st_size > 0 and out.suffix == ".mp3"
    assert not (tmp_path / f"{cache_digest('salt', 'voice', 'hi')}.pcm").exists()


def test_synthesize_zero_byte_cache_remiss(tmp_path, monkeypatch):
    digest = cache_digest("salt", "voice", "hi")
    (tmp_path / f"{digest}.mp3").write_bytes(b"")

    async def fake_key():
        return "k"

    async def fake_pcm(*a, **k):
        return _PCM_SILENCE

    monkeypatch.setattr(mt, "resolve_volc_tts_key", fake_key)
    monkeypatch.setattr(mt, "_synthesize_pcm", fake_pcm)
    out = asyncio.run(synthesize_mp3("hi", "voice", tmp_path, "salt"))
    assert out.stat().st_size > 0


def test_pcm_to_mp3_empty_input_raises(tmp_path):
    bad = tmp_path / "bad.pcm"
    bad.write_bytes(b"")
    with pytest.raises(TTSUnavailableError):
        asyncio.run(pcm_to_mp3(bad, tmp_path / "bad.mp3"))
