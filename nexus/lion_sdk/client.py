"""Lion SDK 客户端：命名空间与配置项读写。"""
from __future__ import annotations

import json
import logging
import os

import httpx

from nexus.sdk_base import BaseAsyncClient
from nexus.service_client import get_service_token

_ALLOWED_ENV_PREFIXES: tuple[str, ...] = (
    "ZHIPU_", "OPENAI_", "LLM_", "EMBEDDING_", "PROMPTFORGE_",
    "LION_", "SMARTLLM_", "DEEPSEEK_", "QWEN_", "GLM_",
    "UC_", "CHROMA_", "BEEMEMORY_", "SPIDER_", "SERVICE_",
    "SECRET_", "ADSMART_", "KDNIAO_", "XIANYU_", "RISK_",
    "NOTIFY_", "FEEDBACK_", "PM_",
)

_GATEWAY_KEY_MAP: dict[str, str] = {
    "chat": "chat.gateway",
    "chat.air": "chat.gateway",
    "chat.advanced": "chat.gateway",
    "embed": "embed.gateway",
    "websearch": "chat.gateway",
    "image": "image",
}


def _resolve_env_refs(parsed: dict[str, object]) -> dict[str, object]:
    """配置值中 ${VAR} 全形式引用按环境白名单前缀解析，未命中置空。"""
    for k, v in parsed.items():
        if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            if not any(env_var.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
                continue
            env_val = os.environ.get(env_var)
            parsed[k] = env_val if env_val else ""
    return parsed


class LionSDK(BaseAsyncClient):
    service_name = "Lion"

    def __init__(
        self,
        base_url: str = "http://${NEXUS_BASE_URL}",
        namespace: str = "default",
        fallback_namespace: str = "default",
        service_token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(base_url, timeout=timeout)
        self._namespace = namespace
        self._fallback_namespace = fallback_namespace
        self._service_token = service_token

    async def _headers(self) -> dict[str, str]:
        token: str = self._service_token or await get_service_token()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, object]:
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            return {"success": False, "detail": f"Unsupported HTTP method: {method}"}
        return await super()._request(method, path, json_data=data, params=params)

    def _envelope(self, response: httpx.Response) -> dict[str, object]:
        data_resp = response.json()
        if data_resp.get("code", 0) != 200:
            return {"success": False, "detail": data_resp.get("message", "unknown error")}
        return data_resp.get("data", {})

    async def get_config(self, group_name: str, key: str) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs/{group_name}/{key}"
        result = await self._request(path)
        if self._is_error(result) and self._namespace != self._fallback_namespace:
            fallback_path = f"/api/v1/namespaces/{self._fallback_namespace}/configs/{group_name}/{key}"
            result = await self._request(fallback_path)
        return result

    async def _get_group_config(
        self,
        group: str,
        key: str,
        *,
        resolve_env: bool = True,
        error_label: str = "",
    ) -> dict[str, object]:
        config = await self.get_config(group, key)
        if self._is_error(config):
            return config
        value = config.get("value", "{}")
        if isinstance(value, str):
            parsed: dict[str, object] = json.loads(value)
        elif isinstance(value, dict):
            parsed = value
        else:
            return {"success": False, "detail": f"Invalid {error_label or group} config value"}
        if resolve_env:
            parsed = _resolve_env_refs(parsed)
        return parsed

    async def get_llm_config(self, key: str = "chat") -> dict[str, object]:
        return await self._get_group_config("llm", key, error_label="llm")

    async def get_chat_config(self) -> dict[str, object]:
        return await self.get_llm_config("chat")

    async def get_embed_config(self) -> dict[str, object]:
        return await self.get_llm_config("embed")

    async def get_image_config(self) -> dict[str, object]:
        return await self.get_llm_config("image")

    async def get_ready_config(self, key: str = "chat", prefer_gateway: bool = True) -> dict[str, object]:
        base_config = await self.get_llm_config(key)
        if self._is_error(base_config) or not prefer_gateway:
            return base_config
        gateway_key = _GATEWAY_KEY_MAP.get(key)
        if gateway_key is None:
            return base_config
        gateway_config = await self.get_llm_config(gateway_key)
        if self._is_error(gateway_config):
            return base_config
        result = dict(base_config)
        result["base_url"] = gateway_config.get("base_url", base_config.get("base_url", ""))
        result["api_key"] = gateway_config.get("api_key", base_config.get("api_key", ""))
        if gateway_config.get("model"):
            result["model"] = gateway_config["model"]
        result["via_gateway"] = True
        result["prompt_manager_ref"] = base_config.get("prompt_manager_ref", "")
        return result

    async def get_infra_config(self, key: str) -> dict[str, object]:
        return await self._get_group_config("infra", key, error_label="infra")

    async def get_business_config(self, key: str) -> dict[str, object]:
        return await self._get_group_config("business", key, resolve_env=False, error_label="business")

_logger = logging.getLogger(__name__)


async def fetch_llm_config(
    key: str = "chat",
    *,
    base_url: str | None = None,
    namespace: str | None = None,
    prefer_gateway: bool = True,
) -> dict[str, object] | None:
    """拉取 Lion LLM 配置：错误信封与异常一律归一为 None，成功返回配置 dict。

    业务仓「Lion 优先、本地 yaml 兜底」的标准入口；连接失败只记日志不抛，
    调用方以 None 判断回退，避免把错误信封误当有效配置消费。
    """
    url = base_url or os.environ.get("LION__BASE_URL", "http://localhost:9527")
    ns = namespace or os.environ.get("LION__NAMESPACE", "default")
    try:
        async with LionSDK(base_url=url, namespace=ns, fallback_namespace="default") as lion:
            result = await lion.get_ready_config(key, prefer_gateway=prefer_gateway)
    except Exception as exc:
        _logger.warning("Lion config unavailable for key=%s: %s", key, exc)
        return None
    if not isinstance(result, dict) or result.get("success") is False:
        _logger.warning("Lion config fetch failed for key=%s: %s", key, result.get("detail") if isinstance(result, dict) else result)
        return None
    return result
