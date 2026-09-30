"""语音转写通用端点: 一行注册 POST {prefix}/voice/transcribe,经 promptManager 网关转写。

业务应用在 main.py 中调用 register_voice_endpoints(app) 即接入语音输入能力；
前端统一用 nux-voice-input 组件上传 WAV,返回 {success, text, duration}。
"""
from __future__ import annotations

import base64

from fastapi import FastAPI, UploadFile
from loguru import logger

MAX_AUDIO_BYTES = 15 * 1024 * 1024


async def _transcribe_upload(file: UploadFile) -> dict[str, object]:
    audio = await file.read()
    if not audio:
        return {"success": False, "detail": "音频内容为空"}
    if len(audio) > MAX_AUDIO_BYTES:
        return {"success": False, "detail": "语音太长，请控制在1分钟内"}
    audio_data = base64.b64encode(audio).decode()
    try:
        from nexus import asr_transcribe
        result = await asr_transcribe(audio_data=audio_data, format="wav", enable_punc=True, timeout=60.0)
    except Exception as e:
        logger.error(f"voice transcribe failed: {type(e).__name__}: {e}")
        return {"success": False, "detail": "语音识别服务暂不可用"}
    if not isinstance(result, dict):
        return {"success": False, "detail": "语音识别服务暂不可用"}
    if result.get("success") is False:
        return {"success": False, "detail": str(result.get("detail") or "语音识别失败")}
    text = str(result.get("text") or "").strip()
    if not text:
        return {"success": False, "detail": "没有听清内容，请再试一次"}
    return {"success": True, "text": text, "duration": int(result.get("duration") or 0)}


def register_voice_endpoints(app: FastAPI, prefix: str = "/api/v1") -> None:
    """注册 POST {prefix}/voice/transcribe（multipart 上传 file 字段）。

    依赖 python-multipart，缺失时跳过注册并告警，不阻断应用启动。
    """
    try:
        __import__("multipart")
    except ImportError:
        logger.warning("python-multipart 未安装，voice/transcribe 未注册（pip install python-multipart）")
        return

    @app.post(f"{prefix}/voice/transcribe")
    async def voice_transcribe(file: UploadFile) -> dict[str, object]:
        return await _transcribe_upload(file)
