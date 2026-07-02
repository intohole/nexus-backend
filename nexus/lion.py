from __future__ import annotations

import asyncio
import time
from typing import Optional

from nexus.config import NexusConfig, get_settings
from nexus.logging import get_logger

logger = get_logger("nexus.lion")

_lion_instance: Optional["LionIntegration"] = None

_CACHE_TTL: int = 300


class LionIntegration:
    def __init__(self, config: Optional[NexusConfig] = None) -> None:
        self._config: NexusConfig = config or get_settings()
        self._cache: dict[str, dict[str, object]] = {}
        self._cache_ts: dict[str, float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_config(
        self,
        key: str,
        prefer_gateway: bool = True,
        use_cache: bool = True,
    ) -> dict[str, object]:
        if use_cache and self._is_cache_valid(key):
            return self._cache[key]

        async with self._lock:
            if use_cache and self._is_cache_valid(key):
                return self._cache[key]

            config: dict[str, object] = await self._fetch_config(key, prefer_gateway)
            if config:
                self._cache[key] = config
                self._cache_ts[key] = time.monotonic()
            return config

    def _is_cache_valid(self, key: str) -> bool:
        if key not in self._cache or key not in self._cache_ts:
            return False
        return (time.monotonic() - self._cache_ts[key]) < _CACHE_TTL

    async def _fetch_config(
        self,
        key: str,
        prefer_gateway: bool,
    ) -> dict[str, object]:
        try:
            from lion_sdk import LionSDK

            lion_cfg = self._config.lion
            async with LionSDK(
                base_url=lion_cfg.base_url,
                namespace=lion_cfg.namespace,
                fallback_namespace="default",
            ) as lion:
                return await lion.get_ready_config(key, prefer_gateway=prefer_gateway)
        except ImportError:
            logger.warning("lion_sdk not installed, Lion integration disabled")
            return {}
        except Exception as exc:
            logger.warning("Lion config fetch failed (key=%s): %s", key, str(exc))
            return {}

    async def get_infra_config(self, key: str, use_cache: bool = True) -> dict[str, object]:
        if use_cache and self._is_cache_valid(key):
            return self._cache[key]

        async with self._lock:
            if use_cache and self._is_cache_valid(key):
                return self._cache[key]

            config: dict[str, object] = await self._fetch_infra_config(key)
            if config and config.get("success") is not False:
                self._cache[key] = config
                self._cache_ts[key] = time.monotonic()
            return config

    async def _fetch_infra_config(self, key: str) -> dict[str, object]:
        try:
            from lion_sdk import LionSDK

            lion_cfg = self._config.lion
            async with LionSDK(
                base_url=lion_cfg.base_url,
                namespace=lion_cfg.namespace,
                fallback_namespace="default",
            ) as lion:
                return await lion.get_infra_config(key)
        except ImportError:
            logger.warning("lion_sdk not installed, Lion integration disabled")
            return {}
        except Exception as exc:
            logger.warning("Lion infra config fetch failed (key=%s): %s", key, str(exc))
            return {}

    async def get_chat_config(self, prefer_gateway: bool = True) -> dict[str, object]:
        return await self.get_config("chat", prefer_gateway=prefer_gateway)

    async def get_embed_config(self, prefer_gateway: bool = True) -> dict[str, object]:
        return await self.get_config("embed", prefer_gateway=prefer_gateway)

    async def get_image_config(self, prefer_gateway: bool = True) -> dict[str, object]:
        return await self.get_config("image", prefer_gateway=prefer_gateway)

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_ts.clear()

    def clear_cache_key(self, key: str) -> None:
        self._cache.pop(key, None)
        self._cache_ts.pop(key, None)


def get_lion() -> LionIntegration:
    global _lion_instance
    if _lion_instance is None:
        _lion_instance = LionIntegration()
    return _lion_instance


async def get_chat_config(prefer_gateway: bool = True) -> dict[str, object]:
    return await get_lion().get_chat_config(prefer_gateway=prefer_gateway)


async def get_embed_config(prefer_gateway: bool = True) -> dict[str, object]:
    return await get_lion().get_embed_config(prefer_gateway=prefer_gateway)


async def get_image_config(prefer_gateway: bool = True) -> dict[str, object]:
    return await get_lion().get_image_config(prefer_gateway=prefer_gateway)


async def get_infra_config(key: str, use_cache: bool = True) -> dict[str, object]:
    return await get_lion().get_infra_config(key, use_cache=use_cache)


def clear_lion_cache() -> None:
    get_lion().clear_cache()
