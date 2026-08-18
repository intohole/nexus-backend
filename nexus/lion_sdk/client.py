from __future__ import annotations

import json
import os

import httpx

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


class LionSDK:

    def __init__(
        self,
        base_url: str = "http://${NEXUS_BASE_URL}",
        namespace: str = "default",
        fallback_namespace: str = "default",
        service_token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._namespace = namespace
        self._fallback_namespace = fallback_namespace
        self._service_token = service_token or os.environ.get("SERVICE_TOKEN")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LionSDK:
        await self._get_client()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        await self.close()

    async def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, object]:
        client = await self._get_client()
        headers: dict[str, str] = {}
        if self._service_token:
            headers["Authorization"] = f"Bearer {self._service_token}"
        try:
            if method == "GET":
                response = await client.get(path, headers=headers, params=params)
            elif method == "POST":
                response = await client.post(path, headers=headers, json=data, params=params)
            elif method == "PUT":
                response = await client.put(path, headers=headers, json=data, params=params)
            elif method == "DELETE":
                response = await client.delete(path, headers=headers, params=params)
            else:
                return {"success": False, "detail": f"Unsupported HTTP method: {method}"}
            data_resp = response.json()
            if data_resp.get("code", 0) != 200:
                return {"success": False, "detail": data_resp.get("message", "unknown error")}
            return data_resp.get("data", {})
        except httpx.ConnectError:
            return {"success": False, "detail": f"Cannot connect to Lion at {self._base_url}"}
        except httpx.TimeoutException:
            return {"success": False, "detail": f"Lion request timeout at {self._base_url}"}
        except httpx.RequestError as e:
            return {"success": False, "detail": f"Lion request error: {e}"}
        except (json.JSONDecodeError, ValueError):
            return {"success": False, "detail": "Lion response parse error"}

    def _is_error(self, result: dict[str, object]) -> bool:
        return result.get("success") is False

    async def get_config(self, group_name: str, key: str) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs/{group_name}/{key}"
        result = await self._request(path)
        if self._is_error(result) and self._namespace != self._fallback_namespace:
            fallback_path = f"/api/v1/namespaces/{self._fallback_namespace}/configs/{group_name}/{key}"
            result = await self._request(fallback_path)
        return result

    async def get_llm_config(self, key: str = "chat") -> dict[str, object]:
        config = await self.get_config("llm", key)
        if self._is_error(config):
            return config
        value = config.get("value", "{}")
        if isinstance(value, str):
            parsed: dict[str, object] = json.loads(value)
        elif isinstance(value, dict):
            parsed = value
        else:
            return {"success": False, "detail": "Invalid llm config value"}
        for k, v in parsed.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                env_var = v[2:-1]
                if not any(env_var.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
                    continue
                env_val = os.environ.get(env_var)
                parsed[k] = env_val if env_val else ""
        return parsed

    async def get_chat_config(self) -> dict[str, object]:
        return await self.get_llm_config("chat")

    async def get_embed_config(self) -> dict[str, object]:
        return await self.get_llm_config("embed")

    async def get_image_config(self) -> dict[str, object]:
        return await self.get_llm_config("image")

    async def get_gateway_chat_config(self) -> dict[str, object]:
        return await self.get_llm_config("chat.gateway")

    async def get_gateway_embed_config(self) -> dict[str, object]:
        return await self.get_llm_config("embed.gateway")

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
        config = await self.get_config("infra", key)
        if self._is_error(config):
            return config
        value = config.get("value", "{}")
        if isinstance(value, str):
            parsed: dict[str, object] = json.loads(value)
        elif isinstance(value, dict):
            parsed = value
        else:
            return {"success": False, "detail": "Invalid infra config value"}
        for k, v in parsed.items():
            if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
                env_var = v[2:-1]
                if not any(env_var.startswith(p) for p in _ALLOWED_ENV_PREFIXES):
                    continue
                env_val = os.environ.get(env_var)
                parsed[k] = env_val if env_val else ""
        return parsed

    async def get_infra_value(self, key: str, field: str = "value") -> str:
        config = await self.get_infra_config(key)
        if self._is_error(config):
            return ""
        val = config.get(field, "")
        return str(val) if val else ""

    async def get_business_config(self, key: str) -> dict[str, object]:
        config = await self.get_config("business", key)
        if self._is_error(config):
            return config
        value = config.get("value", "{}")
        if isinstance(value, str):
            parsed: dict[str, object] = json.loads(value)
        elif isinstance(value, dict):
            parsed = value
        else:
            return {"success": False, "detail": "Invalid business config value"}
        return parsed

    async def get_business_value(self, key: str, field: str, default: str = "") -> str:
        config = await self.get_business_config(key)
        if self._is_error(config):
            return default
        val = config.get(field, default)
        return str(val) if val else default

    async def list_configs(
        self,
        group_name: str | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs?page={page}&page_size={page_size}"
        if group_name:
            path += f"&group_name={group_name}"
        return await self._request(path)

    async def list_namespaces(self) -> list[dict[str, object]]:
        data = await self._request("/api/v1/namespaces")
        if self._is_error(data):
            return []
        if isinstance(data, list):
            return data
        items = data.get("items")
        if isinstance(items, list):
            return items
        return []

    async def create_config(
        self,
        group_name: str,
        key: str,
        value: str,
        description: str = "",
        value_type: str = "json",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "group_name": group_name,
            "key": key,
            "value": value,
            "description": description,
            "value_type": value_type,
        }
        path = f"/api/v1/namespaces/{self._namespace}/configs"
        return await self._request(path, method="POST", data=payload)

    async def update_config(
        self,
        group_name: str,
        key: str,
        value: str,
        description: str = "",
    ) -> dict[str, object]:
        payload: dict[str, object] = {"value": value}
        if description:
            payload["description"] = description
        path = f"/api/v1/namespaces/{self._namespace}/configs/{group_name}/{key}"
        return await self._request(path, method="PUT", data=payload)

    async def delete_config(self, group_name: str, key: str) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs/{group_name}/{key}"
        return await self._request(path, method="DELETE")

    async def search_configs(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs/search"
        return await self._request(path, params={"keyword": keyword, "page": page, "page_size": page_size})

    async def list_groups(self) -> dict[str, object]:
        path = f"/api/v1/namespaces/{self._namespace}/configs/groups"
        return await self._request(path)

    async def create_namespace(
        self,
        name: str,
        display_name: str = "",
        description: str = "",
    ) -> dict[str, object]:
        payload: dict[str, object] = {"name": name}
        if display_name:
            payload["display_name"] = display_name
        if description:
            payload["description"] = description
        return await self._request("/api/v1/namespaces", method="POST", data=payload)

    async def get_namespace(self, name: str) -> dict[str, object]:
        return await self._request(f"/api/v1/namespaces/{name}")

    async def delete_namespace(self, name: str) -> dict[str, object]:
        return await self._request(f"/api/v1/namespaces/{name}", method="DELETE")

    async def get_config_history(
        self,
        namespace: str,
        group: str,
        key: str,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        page = (offset // limit) + 1
        params = {"page": page, "page_size": limit}
        path = f"/api/v1/namespaces/{namespace}/configs/{group}/{key}/history"
        return await self._request(path, params=params)

    async def get_release_diff(
        self,
        release_id_1: int,
        release_id_2: int,
        namespace: str | None = None,
    ) -> dict[str, object]:
        ns = namespace or self._namespace
        path = f"/api/v1/namespaces/{ns}/releases/diff"
        params = {"release_id_1": release_id_1, "release_id_2": release_id_2}
        return await self._request(path, params=params)

    async def rollback_config(
        self,
        namespace: str,
        group: str,
        key: str,
        release_id: int,
        comment: str = "",
    ) -> dict[str, object]:
        path = f"/api/v1/namespaces/{namespace}/configs/{group}/{key}/rollback"
        data = {"release_id": release_id, "comment": comment}
        return await self._request(path, method="POST", data=data)

    async def get_config_tracking(
        self,
        key: str,
        namespace: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        path = f"/api/v1/tracking/key/{key}/history"
        params = {"page": 1, "page_size": limit}
        return await self._request(path, params=params)

    async def get_recent_changes(
        self,
        namespace: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        path = "/api/v1/tracking/recent"
        params = {"page": 1, "page_size": limit}
        if namespace:
            params["namespace"] = namespace
        return await self._request(path, params=params)