"""文生图中间件服务，统一封装 PromptManager 图像生成网关，供各应用复用."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

import httpx

from nexus.lion import get_chat_config, get_image_config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90.0


class ImageService:
    _instance: Optional["ImageService"] = None

    def __new__(cls) -> "ImageService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._base_url = ""
        self._api_key = ""
        self._model = ""
        self._lock = asyncio.Lock()
        self._resolved = False
        self._initialized = True

    async def _resolve_config(self) -> None:
        if self._resolved:
            return
        async with self._lock:
            if self._resolved:
                return
            chat_cfg: Dict = await get_chat_config(prefer_gateway=True)
            image_cfg: Dict = await get_image_config(prefer_gateway=True)
            self._base_url = str(chat_cfg.get("base_url") or self._base_url).rstrip("/")
            self._api_key = str(image_cfg.get("api_key") or chat_cfg.get("api_key") or self._api_key)
            self._model = str(image_cfg.get("model") or self._model)
            self._resolved = True
            logger.info("ImageService resolved: base_url=%s model=%s", self._base_url, self._model)

    async def generate(self, prompt: str, size: str = "1024x1024", n: int = 1) -> str:
        await self._resolve_config()
        if not self._base_url or not self._api_key:
            raise RuntimeError("image gateway config missing (base_url/api_key)")
        url = f"{self._base_url}/images/generations"
        payload = {"prompt": prompt, "size": size, "n": n}
        if self._model:
            payload["model"] = self._model
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"image generation failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        items = data.get("data") or []
        if not items:
            raise RuntimeError("image generation returned no data")
        return str(items[0].get("url") or items[0].get("b64_json") or "")


def get_image_service() -> ImageService:
    return ImageService()