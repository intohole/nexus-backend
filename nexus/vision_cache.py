"""视觉结果 TTL 缓存：同图同问同模型复用，避免重复上传与重复计费。"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

from cachetools import TTLCache

from nexus.llm_config import resolve_app_name

_DEFAULT_MAXSIZE = int(os.environ.get("NEXUS_VISION_CACHE_MAXSIZE", "128"))
_DEFAULT_TTL = int(os.environ.get("NEXUS_VISION_CACHE_TTL", "300"))


class VisionCache:
    def __init__(self, maxsize: int = _DEFAULT_MAXSIZE, ttl: int = _DEFAULT_TTL) -> None:
        self._store: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

    @staticmethod
    def make_key(
        *,
        app: str,
        profile: str,
        detail: str,
        model: str,
        system: str,
        prompt: str,
        image_keys: list[str],
    ) -> str:
        raw = "|".join([
            app or "",
            profile or "",
            detail or "",
            model or "",
            system or "",
            prompt or "",
            ",".join(image_keys),
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def image_key(url: str, digest: str = "") -> str:
        if digest:
            return digest
        return hashlib.sha256(str(url).encode()).hexdigest()[:24]

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        if value:
            self._store[key] = value

    def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        return len(self._store)


_cache: Optional[VisionCache] = None


def get_vision_cache() -> VisionCache:
    global _cache
    if _cache is None:
        _cache = VisionCache()
    return _cache


def build_key(
    *,
    profile: str,
    detail: str,
    model: str,
    system: str,
    prompt: str,
    image_keys: list[str],
) -> str:
    return VisionCache.make_key(
        app=resolve_app_name(),
        profile=profile,
        detail=detail,
        model=model,
        system=system,
        prompt=prompt,
        image_keys=image_keys,
    )


__all__ = ["VisionCache", "build_key", "get_vision_cache"]
