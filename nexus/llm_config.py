"""LLM 网关配置：ironman 客户端初始化、重试策略与网关端点解析。"""
from __future__ import annotations

import asyncio
import os
from typing import Optional, Tuple

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


def effective_retries(max_retries: int) -> int:
    try:
        from nexus.ironman import is_gateway_mode
        if is_gateway_mode():
            return 1
    except ImportError:
        pass
    return max_retries


def resolve_app_name() -> str:
    try:
        from nexus.ironman import get_init_app_name
        name: Optional[str] = get_init_app_name()
        if name:
            return name
    except ImportError:
        pass
    return os.environ.get("APP_NAME", "unknown")


async def resolve_gateway_endpoint(
    model_field: str,
    default_model: str = "",
) -> Tuple[str, str, str]:
    """解析 PromptManager 网关端点，供 vision/image 等多模态服务共用。

    返回 (base_url, api_key, model)；model 取 image 配置的 model_field 字段，
    缺省回退 default_model。
    """
    from nexus.lion import get_chat_config, get_image_config

    chat_cfg: dict = await get_chat_config(prefer_gateway=True)
    image_cfg: dict = await get_image_config(prefer_gateway=True)
    base_url: str = str(chat_cfg.get("base_url") or "").rstrip("/")
    api_key: str = str(image_cfg.get("api_key") or chat_cfg.get("api_key") or "")
    model: str = str(image_cfg.get(model_field) or "") or default_model
    return base_url, api_key, model
