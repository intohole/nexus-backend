"""voice/transcribe 通用端点行为: detail 收敛(内部错误不透传)与 multipart 注册门禁。"""
from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from starlette.datastructures import UploadFile

from nexus.voice import _transcribe_upload, register_voice_endpoints


def _upload(data: bytes = b"x") -> UploadFile:
    return UploadFile(filename="voice.wav", file=io.BytesIO(data))


@pytest.mark.asyncio
async def test_empty_audio_rejected():
    result = await _transcribe_upload(_upload(b""))
    assert result == {"success": False, "detail": "音频内容为空"}


@pytest.mark.asyncio
async def test_upstream_detail_not_leaked():
    upstream = {"success": False, "detail": "promptmanager infra config missing (base_url/api_key)"}
    with patch("nexus.asr.asr_transcribe", return_value=upstream):
        result = await _transcribe_upload(_upload())
    assert result["success"] is False
    assert "promptmanager" not in result["detail"]
    assert result["detail"] == "语音识别服务暂不可用，请稍后再试"


@pytest.mark.asyncio
async def test_success_passthrough():
    upstream = {"text": "你好", "duration": 1200}
    with patch("nexus.asr.asr_transcribe", return_value=upstream):
        result = await _transcribe_upload(_upload())
    assert result == {"success": True, "text": "你好", "duration": 1200}


@pytest.mark.asyncio
async def test_unexpected_upstream_shape():
    with patch("nexus.asr.asr_transcribe", return_value="oops"):
        result = await _transcribe_upload(_upload())
    assert result == {"success": False, "detail": "语音识别服务暂不可用，请稍后再试"}


def test_register_skips_without_multipart():
    from fastapi import FastAPI

    app = FastAPI()
    with patch("builtins.__import__", side_effect=ImportError("no multipart")):
        register_voice_endpoints(app)
    assert not any(getattr(r, "path", "") == "/api/v1/voice/transcribe" for r in app.router.routes)


def test_register_adds_route():
    from fastapi import FastAPI

    app = FastAPI()
    register_voice_endpoints(app)
    assert any(getattr(r, "path", "") == "/api/v1/voice/transcribe" for r in app.router.routes)
