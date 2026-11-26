from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from nexus.vision import VisionService

IMAGE_MEMORY_PREFIX = "[图片记忆]"
DEFAULT_IMAGE_PROMPT = (
    "请客观描述这张图片里可用于长期记忆的关键信息：人物、场景、事件、"
    "偏好、文字要点与可复用事实，不要臆测。"
)
MEDIA_SYSTEM_PROMPT = "你是记忆整理助手，用简体中文输出可长期复用的客观要点。"


class MediaDescriber(Protocol):
    async def describe(self, image: str | bytes, prompt: str = "") -> str:
        ...


class VisionDescriber:
    def __init__(self, vision: VisionService | None = None) -> None:
        self._vision = vision

    def _service(self) -> VisionService:
        if self._vision is not None:
            return self._vision
        from nexus.vision import get_vision_service

        return get_vision_service()

    async def describe(self, image: str | bytes, prompt: str = "") -> str:
        service = self._service()
        text = await service.analyze(
            prompt or DEFAULT_IMAGE_PROMPT,
            image,
            system=MEDIA_SYSTEM_PROMPT,
        )
        return str(text or "").strip()


def compose_image_memory(description: str, prompt: str = "") -> str:
    text = description.strip()
    if not text:
        return ""
    focus = prompt.strip()
    if focus:
        return f"{IMAGE_MEMORY_PREFIX}{focus}：{text}"
    return f"{IMAGE_MEMORY_PREFIX}{text}"


__all__ = [
    "DEFAULT_IMAGE_PROMPT",
    "IMAGE_MEMORY_PREFIX",
    "MEDIA_SYSTEM_PROMPT",
    "MediaDescriber",
    "VisionDescriber",
    "compose_image_memory",
]
