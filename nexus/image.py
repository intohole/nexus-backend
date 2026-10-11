"""文生图中间件服务，统一封装 PromptManager 图像生成网关，供各应用复用.

调用成功后按平台价(feature=image)×张数(cost_factor=n)自动扣积分，
调用前预检（formal 拦截）；积分故障 fail-open 不阻塞生图。
"""

from __future__ import annotations

from typing import Optional

from nexus._gateway_base import _GatewayServiceBase
from nexus.context import get_request_id


class ImageService(_GatewayServiceBase):
    _instance: Optional["ImageService"] = None
    _charge_seq: int = 0

    async def generate(self, prompt: str, size: str = "1024x1024", n: int = 1) -> str:
        await self._resolve_config("model")
        self._require_config("image")
        payload: dict[str, object] = {"prompt": prompt, "size": size, "n": n}
        if self._model:
            payload["model"] = self._model
        await self._preflight(max(1, n))
        data = await self._post(f"{self._base_url}/images/generations", payload, op="image generation")
        items = data.get("data") or []
        if not items:
            raise RuntimeError("image generation returned no data")
        await self._charge(max(1, n))
        return str(items[0].get("url") or items[0].get("b64_json") or "")

    async def _preflight(self, n: int) -> None:
        try:
            from nexus.credits import CreditsInsufficientError, get_credits_service

            await get_credits_service().precheck("image", cost_factor=float(n))
        except Exception as exc:
            from nexus.credits import CreditsInsufficientError

            if isinstance(exc, CreditsInsufficientError):
                raise

    async def _charge(self, n: int) -> None:
        try:
            from nexus.credits import get_credits_service

            type(self)._charge_seq += 1
            await get_credits_service().consume(
                "image",
                ref_id=f"image:{get_request_id() or 'img'}:{type(self)._charge_seq}"[:100],
                description="AI 生图",
                cost_factor=float(n),
            )
        except Exception:
            pass


def get_image_service() -> ImageService:
    return ImageService()
