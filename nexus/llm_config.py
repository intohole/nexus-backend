from __future__ import annotations

import asyncio
import os
from typing import Optional

from nexus.logging import get_logger

logger = get_logger("nexus.llm")

_ironman_configured: bool = False
_ironman_lock: asyncio.Lock = asyncio.Lock()


async def configure_ironman(yaml_path: Optional[str] = None) -> None:
    global _ironman_configured
    if _ironman_configured:
        return

    async with _ironman_lock:
        if _ironman_configured:
            return

        import ironman

        path = yaml_path or os.environ.get("IRONMAN_CONFIG", "")
        if path and os.path.exists(path):
            await ironman.configure(config_path=path)
            logger.info("Ironman configured from %s", path)
        else:
            logger.warning("Ironman config not found, assuming externally configured")
        _ironman_configured = True


def mark_ironman_configured() -> None:
    global _ironman_configured
    _ironman_configured = True


def _effective_retries(max_retries: int) -> int:
    try:
        from nexus.ironman import is_gateway_mode
        if is_gateway_mode():
            return 1
    except ImportError:
        pass
    return max_retries


def _resolve_app_name() -> str:
    try:
        from nexus.ironman import get_init_app_name
        name: Optional[str] = get_init_app_name()
        if name:
            return name
    except ImportError:
        pass
    return os.environ.get("APP_NAME", "unknown")
