"""文生图中间件服务，统一封装 PromptManager 图像生成网关，供各应用复用."""

from __future__ import annotations

from typing import Optional

from nexus._gateway_base import _GatewayServiceBase


class ImageService(_GatewayServiceBase):
    _instance: Optional["ImageService"] = None

    async def generate(self, prompt: str, size: str = "1024x1024", n: int = 1) -> str:
        await self._resolve_config("model")
        self._require_config("image")
        payload: dict[str, object] = {"prompt": prompt, "size": size, "n": n}
        if self._model:
            payload["model"] = self._model
        data = await self._post(f"{self._base_url}/images/generations", payload, op="image generation")
        items = data.get("data") or []
        if not items:
            raise RuntimeError("image generation returned no data")
        return str(items[0].get("url") or items[0].get("b64_json") or "")


def get_image_service() -> ImageService:
    return ImageService()
