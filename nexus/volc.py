"""火山引擎供应链客户端：方舟生图/视频 + 豆包 seed-tts-2.0 双向流式 TTS。

收归自 PiYing 生产验证实现（2026-10-20 真链实锤：Seedream 5.0-flash
参考图一致性、Seedance 图生视频、seed-tts-2.0 V1 二进制协议）。配置经
构造参数注入，应用层负责从 Lion/env 装配；本模块不读任何环境变量。
"""
from __future__ import annotations

import asyncio
import base64
import json
import ssl
import struct
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

_ARK_POLL_INTERVAL = 5.0
_ARK_POLL_TIMEOUT = 900.0
_TTS_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
_TTS_RESOURCE_ID = "seed-tts-2.0"
_TTS_SESSION_TIMEOUT = 120.0
_CONNECTION_EVENTS = (1, 2, 50, 51, 52)


class VolcArkError(Exception):
    pass


class VolcArkUnavailableError(VolcArkError):
    pass


@dataclass
class TTSFrame:
    msg_type: int
    event: Optional[int] = None
    session_id: str = ""
    error_code: Optional[int] = None
    payload: bytes = field(default=b"")


def build_tts_frame(event: int, session_id: str, payload: bytes) -> bytes:
    """V1 协议客户端帧：flag=WithEvent(0b0100)，event 为帧头扩展整数。"""
    header = bytes([0x11, 0x14, 0x10, 0x00])
    buf = struct.pack(">i", event)
    if event not in _CONNECTION_EVENTS:
        sid = session_id.encode("utf-8")
        buf += struct.pack(">I", len(sid)) + sid
    buf += struct.pack(">I", len(payload)) + payload
    return header + buf


def parse_tts_frame(data: bytes) -> TTSFrame:
    if len(data) < 4:
        raise VolcArkError(f"TTS 帧过短: {len(data)}B")
    header_size = (data[0] & 0x0F) * 4
    msg_type = (data[1] >> 4) & 0x0F
    flag = data[1] & 0x0F
    compression = data[2] & 0x0F
    buf = data[header_size:]
    event: Optional[int] = None
    session_id = ""
    error_code: Optional[int] = None
    if flag == 0b0100 and len(buf) >= 4:
        event = struct.unpack(">i", buf[:4])[0]
        buf = buf[4:]
        if event not in _CONNECTION_EVENTS and len(buf) >= 4:
            sid_size = struct.unpack(">I", buf[:4])[0]
            buf = buf[4:]
            if sid_size:
                session_id = buf[:sid_size].decode("utf-8", errors="replace")
                buf = buf[sid_size:]
        elif event in (50, 51, 52) and msg_type == 0b1001 and len(buf) >= 4:
            cid_size = struct.unpack(">I", buf[:4])[0]
            buf = buf[4:]
            if cid_size:
                buf = buf[cid_size:]
    if msg_type == 0b1111 and len(buf) >= 4:
        error_code = struct.unpack(">I", buf[:4])[0]
        buf = buf[4:]
    payload = b""
    if len(buf) >= 4:
        size = struct.unpack(">I", buf[:4])[0]
        payload = buf[4:4 + size]
    if compression == 1 and payload:
        import gzip

        payload = gzip.decompress(payload)
    return TTSFrame(msg_type=msg_type, event=event, session_id=session_id,
                    error_code=error_code, payload=payload)


@dataclass
class ArkResult:
    provider: str
    model: str
    cost_cents: int = 0
    meta: dict = field(default_factory=dict)


class TTSStreamSession:
    """seed-tts-2.0 增量文本流式会话：push_text(句子) → frames() 迭代音频块。

    音频格式默认 pcm(s16le) 便于 WS 网关直推浏览器 AudioContext；
    frames() 在 finish() 之后自然耗尽。会话不可复用，一次一生命周期。
    """

    def __init__(
        self,
        api_key: str,
        voice: str = "zh_female_gaolengyujie_uranus_bigtts",
        sample_rate: int = 16000,
        audio_format: str = "pcm",
        uid: str = "nexus-volc",
        frame_timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._voice = voice
        self._sample_rate = sample_rate
        self._audio_format = audio_format
        self._uid = uid
        self._frame_timeout = frame_timeout
        self._ws: object | None = None
        self._session_id = ""
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        if not self._api_key:
            raise VolcArkUnavailableError("volc.tts_api_key 未配置（Lion 或 env）")
        try:
            import websockets
        except ImportError as exc:
            raise VolcArkError("websockets 依赖未安装，TTS 不可用") from exc
        ctx = ssl.create_default_context()
        self._session_id = uuid.uuid4().hex
        self._ws = await websockets.connect(
            _TTS_WS_URL,
            additional_headers={
                "X-Api-Key": self._api_key,
                "X-Api-Resource-Id": _TTS_RESOURCE_ID,
                "X-Api-Connect-Id": str(uuid.uuid4()),
            },
            ssl=ctx,
            max_size=20 * 1024 * 1024,
            open_timeout=15,
            close_timeout=5,
        )
        req_params = {
            "speaker": self._voice,
            "audio_params": {"format": self._audio_format, "sample_rate": self._sample_rate},
        }
        base_payload = {
            "user": {"uid": self._uid},
            "namespace": "BidirectionalTTS",
            "req_params": req_params,
        }
        await self._ws.send(build_tts_frame(1, "", b"{}"))
        await self._expect_event(self._ws, 50)
        start_payload = dict(base_payload, event=100)
        await self._ws.send(build_tts_frame(
            100, self._session_id, json.dumps(start_payload, ensure_ascii=False).encode()
        ))
        await self._expect_event(self._ws, 150)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def push_text(self, text: str) -> None:
        if self._ws is None or self._closed:
            raise VolcArkError("TTS 会话未建立或已关闭")
        task_payload = {
            "user": {"uid": self._uid},
            "namespace": "BidirectionalTTS",
            "event": 200,
            "req_params": {
                "speaker": self._voice,
                "audio_params": {"format": self._audio_format, "sample_rate": self._sample_rate},
                "text": text[:2000],
            },
        }
        await self._ws.send(build_tts_frame(
            200, self._session_id, json.dumps(task_payload, ensure_ascii=False).encode()
        ))

    async def finish(self) -> None:
        if self._ws is not None and not self._closed:
            await self._ws.send(build_tts_frame(102, self._session_id, b"{}"))

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

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

    async def _reader_loop(self) -> None:
        try:
            while True:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self._frame_timeout)
                frame = parse_tts_frame(bytes(raw))
                if frame.msg_type == 0b1111:
                    detail = frame.payload.decode(errors="replace")[:300]
                    raise VolcArkError(f"豆包TTS 错帧 code={frame.error_code}: {detail}")
                if frame.msg_type == 0b1011:
                    if frame.payload:
                        self._queue.put_nowait(frame.payload)
                    continue
                if frame.event in (152, 52):
                    return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if not self._closed:
                logger.warning("tts stream reader frame timeout")
        except VolcArkError as e:
            if not self._closed:
                logger.warning("tts stream reader error: %s", e)
        except Exception as e:
            if not self._closed:
                logger.warning("tts stream reader exit: %s", e)
        finally:
            self._queue.put_nowait(None)

    @staticmethod
    async def _expect_event(ws: object, expected: int) -> None:
        deadline = asyncio.get_running_loop().time() + 30.0
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise VolcArkError(f"豆包TTS 等待事件 {expected} 超时")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            frame = parse_tts_frame(bytes(raw))
            if frame.msg_type == 0b1111:
                detail = frame.payload.decode(errors="replace")[:300]
                raise VolcArkError(f"豆包TTS 错帧 code={frame.error_code}: {detail}")
            if frame.event == expected:
                return
            if frame.event in (51, 153):
                raise VolcArkError(f"豆包TTS 会话被拒: event={frame.event}")


class VolcArkClient:
    """方舟生图/视频 + seed-tts-2.0 语音合成。参数全部构造注入。"""

    def __init__(
        self,
        ark_api_key: str = "",
        ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        seedream_model: str = "doubao-seedream-4-0-250828",
        seedance_model: str = "doubao-seedance-1-0-pro-250528",
        tts_api_key: str = "",
        tts_voice: str = "zh_female_gaolengyujie_uranus_bigtts",
        tts_sample_rate: int = 24000,
    ) -> None:
        self.ark_api_key = ark_api_key
        self.ark_base_url = ark_base_url
        self.seedream_model = seedream_model
        self.seedance_model = seedance_model
        self.tts_api_key = tts_api_key
        self.tts_voice = tts_voice
        self.tts_sample_rate = tts_sample_rate

    def _require_ark(self) -> str:
        if not self.ark_api_key:
            raise VolcArkUnavailableError("volc.ark_api_key 未配置（Lion 或 env）")
        return self.ark_api_key

    def _to_data_url(self, path: Path) -> str:
        suffix = path.suffix.lstrip(".") or "png"
        encoded = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/{suffix};base64,{encoded}"

    async def generate_image(
        self,
        prompt: str,
        out_path: Path,
        ref_images: Optional[list[Path]] = None,
    ) -> ArkResult:
        api_key = self._require_ark()
        payload: dict = {
            "model": self.seedream_model,
            "prompt": prompt,
            "size": "2K",
            "response_format": "b64_json",
            "watermark": False,
        }
        if ref_images:
            payload["image"] = [self._to_data_url(p) for p in ref_images]
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{self.ark_base_url}/images/generations",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            body = response.json()
            if response.status_code != 200:
                raise VolcArkError(f"Seedream 生图失败: {body}")
            data = (body.get("data") or [{}])[0]
            b64 = data.get("b64_json") or ""
            if not b64:
                raise VolcArkError("Seedream 返回空图")
            await asyncio.to_thread(out_path.write_bytes, base64.b64decode(b64))
        return ArkResult(provider="seedream", model=self.seedream_model, cost_cents=21)

    async def generate_video(
        self, prompt: str, image_path: Path, out_path: Path, duration: int = 5
    ) -> ArkResult:
        api_key = self._require_ark()
        image_url = self._to_data_url(image_path)
        text = f"{prompt} --ratio adaptive --duration {duration}"
        async with httpx.AsyncClient(timeout=60.0) as client:
            submit = await client.post(
                f"{self.ark_base_url}/contents/generations/tasks",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": self.seedance_model,
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            )
            body = submit.json()
            if submit.status_code != 200:
                raise VolcArkError(f"Seedance 提交失败: {body}")
            task_id = body.get("id") or ""
            if not task_id:
                raise VolcArkError("Seedance 无任务 id")
            video_url = await self._poll_ark_task(client, api_key, task_id)
            downloaded = await client.get(video_url)
            downloaded.raise_for_status()
            await asyncio.to_thread(out_path.write_bytes, downloaded.content)
        return ArkResult(provider="seedance", model=self.seedance_model, cost_cents=500)

    async def _poll_ark_task(
        self, client: httpx.AsyncClient, api_key: str, task_id: str
    ) -> str:
        deadline = time.monotonic() + _ARK_POLL_TIMEOUT
        while time.monotonic() < deadline:
            detail = await client.get(
                f"{self.ark_base_url}/contents/generations/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            body = detail.json()
            status = body.get("status") or ""
            if status == "succeeded":
                url = ((body.get("content") or {}).get("video_url")) or ""
                if not url:
                    raise VolcArkError(f"Seedance 成功但无视频: {body}")
                return url
            if status in ("failed", "cancelled"):
                raise VolcArkError(f"Seedance 任务终止: {body}")
            await asyncio.sleep(_ARK_POLL_INTERVAL)
        raise VolcArkError(f"Seedance 轮询超时 task={task_id}")

    async def synthesize_speech(self, text: str, out_path: Path) -> ArkResult:
        if not self.tts_api_key:
            raise VolcArkUnavailableError("volc.tts_api_key 未配置（Lion 或 env）")
        session = TTSStreamSession(
            api_key=self.tts_api_key, voice=self.tts_voice, sample_rate=self.tts_sample_rate,
            audio_format="mp3",
        )
        await session.start()
        audio: list[bytes] = []
        try:
            await session.push_text(text[:2000])
            await session.finish()
            async for chunk in session.frames():
                audio.append(chunk)
        finally:
            await session.close()
        if not audio:
            raise VolcArkError("豆包TTS 会话完成但无音频数据")
        await asyncio.to_thread(out_path.write_bytes, b"".join(audio))
        return ArkResult(provider="doubao", model=_TTS_RESOURCE_ID, cost_cents=1)
