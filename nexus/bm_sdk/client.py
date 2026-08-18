import os
import logging
import httpx
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_UNAVAILABLE_RESULT: Dict[str, bool] = {"success": False, "unavailable": True}


class BeeMemorySDK:
    def __init__(
        self,
        base_url: str = "",
        service_token: Optional[str] = None,
        app_name: str = "default",
        timeout: float = 10.0,
    ):
        if not base_url:
            base_url = os.environ.get("BEEMEMORY_BASE_URL", "${BEE_MEMORY_BASE_URL}")
        self.base_url: str = base_url.rstrip("/")
        self.service_token: Optional[str] = service_token or os.environ.get("SERVICE_TOKEN")
        self.app_name: str = app_name
        self._timeout: float = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: Dict[str, str] = {"Content-Type": "application/json"}
            if self.service_token:
                headers["X-Service-Token"] = self.service_token
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers=headers,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "BeeMemorySDK":
        await self._get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict:
        if data and "user_id" in data:
            data["user_id"] = str(data["user_id"])
        if params and "user_id" in params:
            params["user_id"] = str(params["user_id"])

        client = await self._get_client()
        try:
            response = await client.request(method, endpoint, json=data, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 501):
                logger.warning("beeMemory服务不可用 (HTTP %d)", e.response.status_code)
                return {**_UNAVAILABLE_RESULT, "message": f"HTTP {e.response.status_code}"}
            logger.warning("beeMemory HTTP错误: %d - %s", e.response.status_code, e.response.text[:200])
            return {"success": False, "message": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except httpx.RequestError as e:
            logger.warning("beeMemory连接失败: %s", type(e).__name__)
            return {**_UNAVAILABLE_RESULT, "message": str(e)}
        except Exception as e:
            logger.error("beeMemory未知错误: %s", e)
            return {"success": False, "message": str(e)}

    async def add_memory(
        self,
        content: str,
        user_id: str,
        app_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict:
        payload: Dict[str, str] = {
            "content": content,
            "user_id": user_id,
            "app_name": app_name or self.app_name,
        }
        if session_id:
            payload["session_id"] = session_id
        return await self._request("POST", "/api/memory/v1/add", data=payload)

    async def search_memories(
        self,
        query: str,
        user_id: str,
        app_name: Optional[str] = None,
        top_k: int = 5,
    ) -> Dict:
        payload: Dict = {
            "query": query,
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "top_k": top_k,
        }
        return await self._request("POST", "/api/memory/v1/search", data=payload)

    async def recall_memories(
        self,
        content: str,
        user_id: str,
        app_name: Optional[str] = None,
        top_k: int = 5,
        smart: bool = True,
    ) -> Dict:
        payload: Dict = {
            "content": content,
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "top_k": top_k,
            "smart": smart,
        }
        return await self._request("POST", "/api/memory/v1/recall", data=payload)

    async def list_memories(
        self,
        user_id: str,
        app_name: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict:
        params: Dict[str, str] = {
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "page": str(page),
            "page_size": str(page_size),
        }
        return await self._request("GET", "/api/memory/v1/list", params=params)

    async def delete_memory(self, memory_id: str) -> Dict:
        return await self._request("DELETE", f"/api/memory/v1/{memory_id}")

    async def save_history(
        self,
        user_id: str,
        messages: List[Dict],
        app_name: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict:
        payload: Dict = {
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "messages": messages,
        }
        if session_id:
            payload["session_id"] = session_id
        return await self._request("POST", "/api/memory/submit", data=payload)

    async def ensure_app_config(
        self,
        app_name: Optional[str] = None,
        **kwargs,
    ) -> Dict:
        payload: Dict = {"app_name": app_name or self.app_name}
        payload.update(kwargs)
        return await self._request("POST", "/api/memory/app/config/ensure", data=payload)

    async def health_check(self) -> bool:
        try:
            client = await self._get_client()
            response = await client.get("/api/health", timeout=3.0)
            if response.status_code == 200:
                data = response.json()
                return data.get("success", False) or data.get("status") == "healthy"
            return False
        except Exception:
            return False