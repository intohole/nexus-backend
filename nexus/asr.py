"""录音转写(ASR)统一客户端: 经 promptManager 网关调用,屏蔽厂商差异。

配置来自 Lion 基建 promptmanager 条目(base_url/api_key),业务应用直接调用：
    from nexus import asr_transcribe
    result = await asr_transcribe(audio_url="https://.../voice.mp3")
返回 {text, duration, utterances, _gateway}；失败返回 {success: False, detail}。
"""
from __future__ import annotations

import httpx

from nexus.infra import get_promptmanager_config
from nexus.logging import get_logger

logger = get_logger("nexus.asr")

_API_PREFIX = "/api/gateway/v1"
_TRANSCRIBE_PATH = _API_PREFIX + "/audio/transcriptions"
_MAX_RETRIES = 2


async def asr_transcribe(
    audio_url: str | None = None,
    audio_data: str | None = None,
    model: str | None = None,
    format: str | None = None,
    codec: str | None = None,
    language: str | None = None,
    enable_itn: bool | None = None,
    enable_punc: bool | None = None,
    enable_ddc: bool | None = None,
    enable_speaker_info: bool | None = None,
    show_utterances: bool | None = None,
    timeout: float = 120.0,
) -> dict[str, object]:
    if not audio_url and not audio_data:
        return {"success": False, "detail": "audio_url or audio_data is required"}
    config = await get_promptmanager_config()
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not base_url or not api_key:
        return {"success": False, "detail": "promptmanager infra config missing (base_url/api_key)"}
    if base_url.endswith(_API_PREFIX):
        base_url = base_url[: -len(_API_PREFIX)]

    payload: dict[str, object] = {}
    if audio_url is not None:
        payload["audio_url"] = audio_url
    if audio_data is not None:
        payload["audio_data"] = audio_data
    for key, value in (
        ("model", model), ("format", format), ("codec", codec), ("language", language),
        ("enable_itn", enable_itn), ("enable_punc", enable_punc), ("enable_ddc", enable_ddc),
        ("enable_speaker_info", enable_speaker_info), ("show_utterances", show_utterances),
    ):
        if value is not None:
            payload[key] = value

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_detail = ""
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
                response = await client.post(f"{base_url}{_TRANSCRIBE_PATH}", json=payload, headers=headers)
            data = response.json()
            if response.status_code == 200 and "error" not in data:
                return data
            last_detail = str(data.get("error", {}).get("message") or data)[:300] if isinstance(data, dict) else str(data)[:300]
            if response.status_code < 500 and "error" in data:
                return {"success": False, "detail": last_detail}
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_detail = f"{type(e).__name__}: {e}"
        except ValueError:
            return {"success": False, "detail": "gateway response parse error"}
        if attempt < _MAX_RETRIES - 1:
            logger.warning("ASR transcribe retry %s: %s", attempt + 1, last_detail)
    return {"success": False, "detail": last_detail or "gateway unavailable"}
