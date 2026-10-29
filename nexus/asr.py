"""录音转写(ASR)统一客户端: 经 promptManager 网关调用,屏蔽厂商差异。

配置来自 Lion 基建 promptmanager 条目(base_url/api_key),业务应用直接调用：
    from nexus import asr_transcribe
    result = await asr_transcribe(audio_url="https://.../voice.mp3")
返回 {text, duration, utterances, _gateway}；失败返回 {success: False, detail}。

流式识别（实时对话场景）用 AsrStreamSession：直连火山大模型流式识别 WSS，
密钥由业务侧注入（长连接不过网关）：
    session = AsrStreamSession(api_key=...)
    await session.start()
    await session.push_audio(pcm_chunk)
    async for r in session.results(): ...
"""
from __future__ import annotations

import asyncio
import gzip
import json
import struct
import uuid
from collections.abc import AsyncIterator

import httpx

from nexus.infra import get_promptmanager_config
from nexus.logging import get_logger

logger = get_logger("nexus.asr")

_API_PREFIX = "/api/gateway/v1"
_TRANSCRIBE_PATH = _API_PREFIX + "/audio/transcriptions"
_MAX_RETRIES = 2

_ASR_WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
_DEFAULT_ASR_RESOURCE = "volc.seedasr.sauc.duration"
_ASR_AUDIO_CHUNK_MS = 100


class AsrStreamError(Exception):
    pass


class AsrStreamSession:
    """大模型流式语音识别会话（双向流式优化版）。

    16kHz mono s16le PCM 分包推送（建议 100~200ms/包），results() 迭代识别
    结果 {text, definite, utterances, full_text}；definite=True 为判停分句。
    """

    def __init__(
        self,
        api_key: str,
        resource_id: str = _DEFAULT_ASR_RESOURCE,
        sample_rate: int = 16000,
        uid: str = "nexus",
        end_window_ms: int = 800,
        enable_nonstream: bool = True,
    ) -> None:
        self._api_key = api_key
        self._resource_id = resource_id
        self._sample_rate = sample_rate
        self._uid = uid
        self._end_window_ms = max(end_window_ms, 200)
        self._enable_nonstream = enable_nonstream
        self._ws: object | None = None
        self._seq = 0
        self._queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        if not self._api_key:
            raise AsrStreamError("asr api_key 未配置")
        try:
            import ssl
            import websockets
        except ImportError as exc:
            raise AsrStreamError("websockets 依赖未安装，流式识别不可用") from exc
        self._ws = await websockets.connect(
            _ASR_WS_URL,
            additional_headers={
                "X-Api-Key": self._api_key,
                "X-Api-Resource-Id": self._resource_id,
                "X-Api-Request-Id": str(uuid.uuid4()),
                "X-Api-Sequence": "-1",
                "X-Api-Connect-Id": str(uuid.uuid4()),
            },
            ssl=ssl.create_default_context(),
            max_size=8 * 1024 * 1024,
            open_timeout=10,
            close_timeout=5,
        )
        request_payload = {
            "user": {"uid": self._uid},
            "audio": {"format": "pcm", "rate": self._sample_rate, "bits": 16, "channel": 1},
            "request": {
                "model_name": "bigmodel",
                "enable_punc": True,
                "show_utterances": True,
                "result_type": "full",
                "end_window_size": self._end_window_ms,
                "enable_nonstream": self._enable_nonstream,
                "enable_accelerate_text": True,
            },
        }
        await self._send_frame(0b0001, 0b0000, gzip.compress(json.dumps(request_payload).encode()))
        self._seq = 1
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def push_audio(self, pcm: bytes, last: bool = False) -> None:
        if self._ws is None or self._closed:
            raise AsrStreamError("会话未建立或已关闭")
        self._seq += 1
        flags = 0b0011 if last else 0b0001
        seq = -self._seq if last else self._seq
        await self._send_frame(0b0010, flags, gzip.compress(pcm), seq=seq)

    async def finish(self) -> None:
        if self._ws is not None and not self._closed:
            self._seq += 1
            await self._send_frame(0b0010, 0b0011, gzip.compress(b""), seq=-self._seq)

    async def results(self) -> AsyncIterator[dict[str, object]]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send_frame(self, msg_type: int, flags: int, payload: bytes, seq: int = 0) -> None:
        header = bytes([0x11, (msg_type << 4) | flags, 0x11, 0x00])
        ext = struct.pack(">i", seq) if flags & 0b0011 else b""
        await self._ws.send(header + ext + struct.pack(">I", len(payload)) + payload)

    async def _reader_loop(self) -> None:
        full_text = ""
        try:
            while True:
                raw = await self._ws.recv()
                frame = self._parse_frame(bytes(raw))
                if frame["msg_type"] == 0b1111:
                    self._queue.put_nowait({"error": frame["payload"].decode(errors="replace")[:300]})
                    return
                if frame["msg_type"] != 0b1001:
                    continue
                try:
                    body = json.loads(frame["payload"].decode())
                except ValueError:
                    continue
                result = body.get("result") or {}
                text = str(result.get("text") or "")
                utterances = result.get("utterances") or []
                definite_parts = [str(u.get("text") or "") for u in utterances if u.get("definite")]
                if definite_parts:
                    full_text += "".join(definite_parts)
                item: dict[str, object] = {
                    "text": text,
                    "definite": bool(definite_parts),
                    "definite_text": "".join(definite_parts),
                    "utterances": utterances,
                    "full_text": full_text,
                }
                self._queue.put_nowait(item)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closed:
                logger.warning("asr stream reader exit: %s", e)
        finally:
            self._queue.put_nowait(None)

    @staticmethod
    def _parse_frame(data: bytes) -> dict[str, object]:
        if len(data) < 8:
            return {"msg_type": 0, "payload": b""}
        msg_type = (data[1] >> 4) & 0x0F
        flags = data[1] & 0x0F
        compression = data[2] & 0x0F
        buf = data[(data[0] & 0x0F) * 4:]
        if flags & 0b0011 and len(buf) >= 4:
            buf = buf[4:]
        size = struct.unpack(">I", buf[:4])[0]
        payload = buf[4:4 + size]
        if compression == 1 and payload:
            payload = gzip.decompress(payload)
        return {"msg_type": msg_type, "payload": payload}


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
