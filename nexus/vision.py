"""多模态视觉识别服务，统一封装 PromptManager 网关视觉能力（GLM-4.6V-Flash/GLM-4.5V），供各应用复用.

通用能力：单图/多图识别（recognize）、结构化 JSON 识别（recognize_json）、
本地图片字节识别（recognize_bytes，base64 data URL 直传，网关无需可访问图片地址）。
"""

from __future__ import annotations

import base64
from typing import Iterable, Optional, Sequence, Union

from nexus._gateway_base import _GatewayServiceBase
from nexus.llm_utils import parse_llm_json

DEFAULT_VISION_MODEL = "glm-4.6v-flash"
JSON_HINT = "只输出一个 JSON 对象，不要包含解释、markdown 代码块或其他文字。"

ImageInput = Union[str, bytes, bytearray]

_MIME_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(data: bytes) -> str:
    for magic, mime in _MIME_MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    raise ValueError("无法识别的图片格式（仅支持 png/jpeg/gif/webp/bmp）")


def to_data_url(data: bytes, mime_type: str | None = None) -> str:
    """图片字节 → base64 data URL（OpenAI 兼容网关均可识别）。"""
    mime = mime_type or sniff_image_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


GATEWAY_VISION_PATH = "/chat/completions"


class VisionService(_GatewayServiceBase):
    _instance: Optional["VisionService"] = None

    @property
    def model(self) -> str:
        return self._model or DEFAULT_VISION_MODEL

    def _normalize_images(self, images: Union[ImageInput, Iterable[ImageInput]]) -> list[str]:
        """URL/data URL/bytes → image_url 列表；单张图片可直接传 str/bytes。"""
        if isinstance(images, (str, bytes, bytearray)):
            items: Sequence[ImageInput] = [images]
        else:
            items = list(images)
        if not items:
            raise ValueError("images 不能为空")
        urls: list[str] = []
        for item in items:
            if isinstance(item, (bytes, bytearray)):
                urls.append(to_data_url(bytes(item)))
            elif isinstance(item, str):
                urls.append(item)
            else:
                raise TypeError(f"不支持的图片来源类型: {type(item).__name__}")
        return urls

    async def _chat_vision(
        self,
        system: str,
        prompt: str,
        image_urls: list[str],
        temperature: float,
    ) -> str:
        await self._resolve_config("vision_model", DEFAULT_VISION_MODEL)
        self._require_config("vision")
        content: list[dict[str, object]] = [
            {"type": "image_url", "image_url": {"url": image_url}} for image_url in image_urls
        ]
        content.append({"type": "text", "text": prompt})
        payload = {
            "model": self._model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        }
        data = await self._post(f"{self._base_url}{GATEWAY_VISION_PATH}", payload, op="vision request")
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("vision request returned no choices")
        message = choices[0].get("message") or {}
        content_text = str(message.get("content") or "")
        if not content_text.strip():
            raise RuntimeError("vision request returned empty content")
        return content_text

    async def recognize(
        self,
        prompt: str,
        images: Union[ImageInput, Iterable[ImageInput]],
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        """通用图片识别：对单张/多张图片提问，返回模型文本回答。

        images 元素支持图片 URL、base64 data URL 或原始字节（bytes 自动转 data URL）。
        """
        image_urls = self._normalize_images(images)
        return await self._chat_vision(system or "你是图片识别助手，用简体中文准确回答。", prompt, image_urls, temperature)

    async def recognize_json(
        self,
        prompt: str,
        images: Union[ImageInput, Iterable[ImageInput]],
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> dict[str, object]:
        """结构化图片识别：要求模型输出 JSON 并解析为 dict，失败抛 RuntimeError。"""
        image_urls = self._normalize_images(images)
        merged_system = f"{system or '你是图片识别助手。'}\n{JSON_HINT}"
        raw = await self._chat_vision(f"{merged_system}", prompt, image_urls, temperature)
        result = parse_llm_json(raw)
        # parse_llm_json 失败时返回 {"raw_response": ...} 兜底；结构化接口对该兜底显式报错
        if not result or set(result) == {"raw_response"}:
            raise RuntimeError(f"vision recognize_json returned invalid JSON: {raw[:200]}")
        return result

    async def recognize_bytes(
        self,
        prompt: str,
        blobs: Union[bytes, bytearray, Iterable[Union[bytes, bytearray]]],
        system: Optional[str] = None,
        temperature: float = 0.2,
    ) -> str:
        """本地图片字节识别的语义化入口（等价 recognize + 自动 data URL 转换）。"""
        return await self.recognize(prompt, blobs, system=system, temperature=temperature)


def get_vision_service() -> VisionService:
    return VisionService()


__all__ = [
    "VisionService",
    "get_vision_service",
    "sniff_image_mime",
    "to_data_url",
    "DEFAULT_VISION_MODEL",
]
