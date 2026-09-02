"""多模态视觉审查中间件服务，统一封装 PromptManager 网关视觉能力（GLM-4.6V-Flash/GLM-4.5V），供各应用复用."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

import httpx

from nexus.lion import get_chat_config, get_image_config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90.0
DEFAULT_VISION_MODEL = "glm-4.6v-flash"


class VisionService:
    _instance: Optional["VisionService"] = None

    def __new__(cls) -> "VisionService":
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
            self._model = str(image_cfg.get("vision_model") or self._model or DEFAULT_VISION_MODEL)
            self._resolved = True
            logger.info("VisionService resolved: base_url=%s model=%s", self._base_url, self._model)

    async def review(self, system: str, prompt: str, image_url: str, temperature: float = 0.2) -> str:
        await self._resolve_config()
        if not self._base_url or not self._api_key:
            raise RuntimeError("vision gateway config missing (base_url/api_key)")
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"vision review failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("vision review returned no choices")
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        if not content.strip():
            raise RuntimeError("vision review returned empty content")
        return content


def get_vision_service() -> VisionService:
    return VisionService()
