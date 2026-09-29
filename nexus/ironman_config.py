"""ironman 配置加载：从 Lion 拉取 chat/embed 配置，拒绝环境变量兜底。"""
from __future__ import annotations

from typing import Awaitable, Callable

from nexus.lion import get_chat_config, get_embed_config
from nexus.logging import get_logger

logger = get_logger("nexus.ironman")

ConfigLoader = Callable[[str], Awaitable[dict[str, object]]]

# P2: 网关模式标志（由 default_config_loader 写入，is_gateway_mode() 读取）
_via_gateway: bool = False


class IronmanConfigError(RuntimeError):
    pass


def _is_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _clean(value: str, *fallbacks: str) -> str:
    if value and not _is_placeholder(value):
        return value
    for fb in fallbacks:
        if fb and not _is_placeholder(fb):
            return fb
    return ""


async def default_config_loader(app_name: str) -> dict[str, object]:
    """ironman Bootstrap 默认配置加载器。

    配置唯一来源：Lion（nexus.lion.get_chat_config / get_embed_config）。
    不再从环境变量兜底（PROMPTFORGE_API_KEY / LLM_API_KEY 等），
    避免配置散落在环境变量中难以管控。LLM 连接配置（api_key/base_url）
    缺失时直接抛错，拒绝静默进入降级模式掩盖问题。
    """
    global _via_gateway
    chat_cfg = await get_chat_config(prefer_gateway=True)
    _via_gateway = bool(chat_cfg.get("via_gateway", False))
    embed_cfg = await get_embed_config(prefer_gateway=True)

    api_key = _clean(str(chat_cfg.get("api_key", "")))
    base_url = _clean(str(chat_cfg.get("base_url", "")))
    model = _clean(str(chat_cfg.get("model", "")))
    provider = str(chat_cfg.get("provider", "") or "openai")

    if not api_key or not base_url:
        raise IronmanConfigError(
            f"Lion 未返回 {app_name} 可用的 LLM chat 配置（api_key/base_url 为空）。"
            f"请检查 LION_NAMESPACE={app_name} 的 llm/chat 配置是否存在、Lion 服务是否可达、"
            f"SERVICE_TOKEN 是否注入应用环境。收到配置: {chat_cfg or '{}'}"
        )

    emb_api_key = _clean(str(embed_cfg.get("api_key", ""))) or api_key
    emb_base_url = _clean(str(embed_cfg.get("base_url", ""))) or base_url
    emb_model = _clean(str(embed_cfg.get("model", "")))
    emb_provider = str(embed_cfg.get("provider", "") or provider)
    emb_dim = embed_cfg.get("dimensions") or embed_cfg.get("dimension") or 0

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "provider": provider,
        "embedding_api_key": emb_api_key,
        "embedding_base_url": emb_base_url,
        "embedding_model": emb_model,
        "embedding_provider": emb_provider,
        "embedding_dimensions": int(emb_dim),
    }


def is_gateway_mode() -> bool:
    """P2: 当前 ironman 是否通过 prompt-manager 网关模式调用。

    由 default_config_loader 写入。用于 LLMService 判断是否降低重试次数
    （网关已有 failover，无需业务层多重试）。
    """
    return _via_gateway
