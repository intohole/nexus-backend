"""beeMemory SDK 客户端：记忆读写与检索调用。"""
import os
import logging
import httpx
from typing import Dict, List, Optional

from nexus.infra import get_beememory_base_url
from nexus.sdk_base import BaseAsyncClient
from nexus.service_client import get_service_token

logger = logging.getLogger(__name__)

_UNAVAILABLE_RESULT: Dict[str, bool] = {"success": False, "unavailable": True}


class BeeMemorySDK(BaseAsyncClient):
    service_name = "beeMemory"

    def __init__(
        self,
        base_url: str = "",
        service_token: Optional[str] = None,
        app_name: str = "default",
        timeout: float = 10.0,
    ):
        super().__init__(base_url, timeout=timeout)
        self.service_token: Optional[str] = service_token
        self.app_name: str = app_name

    async def _resolve_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        url = await get_beememory_base_url()
        if not url:
            url = os.environ.get("BEEMEMORY_BASE_URL", "") or "http://localhost:8700"
        self.base_url = url.rstrip("/")
        return self.base_url

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

        await self._resolve_base_url()
        client = await self._get_client()
        headers: Dict[str, str] = {}
        token: str = self.service_token or await get_service_token()
        if token:
            headers["X-Service-Token"] = token
        try:
            response = await client.request(method, endpoint, headers=headers, json=data, params=params)
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
        channel: Optional[str] = None,
    ) -> Dict:
        payload: Dict[str, str] = {
            "content": content,
            "user_id": user_id,
            "app_name": app_name or self.app_name,
        }
        if session_id:
            payload["session_id"] = session_id
        if channel:
            payload["channel"] = channel
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
        max_chars: Optional[int] = None,
    ) -> Dict:
        payload: Dict = {
            "content": content,
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "top_k": top_k,
            "smart": smart,
        }
        if max_chars is not None:
            payload["max_chars"] = max_chars
        return await self._request("POST", "/api/memory/v1/recall", data=payload)

    async def snapshot_memories(
        self,
        user_id: str,
        app_name: Optional[str] = None,
        top_k: int = 10,
        max_chars: Optional[int] = None,
    ) -> Dict:
        payload: Dict = {
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "top_k": top_k,
        }
        if max_chars is not None:
            payload["max_chars"] = max_chars
        return await self._request("POST", "/api/memory/v1/snapshot", data=payload)

    async def get_context(
        self,
        user_id: str,
        app_name: Optional[str] = None,
        session_id: Optional[str] = None,
        history_turns: int = 12,
        history_max_chars: Optional[int] = None,
        top_k: int = 10,
        max_chars: Optional[int] = None,
    ) -> Dict:
        """工作上下文聚合（r27）：近 N 条对话窗口 + 热记忆快照一次装填（确定性 0 LLM）。"""
        payload: Dict = {
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "history_turns": history_turns,
            "top_k": top_k,
        }
        if session_id:
            payload["session_id"] = session_id
        if history_max_chars is not None:
            payload["history_max_chars"] = history_max_chars
        if max_chars is not None:
            payload["max_chars"] = max_chars
        return await self._request("POST", "/api/memory/v1/context", data=payload)

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

    async def pin_memory(self, memory_id: str) -> Dict:
        """钉住（core memory 块）：钉住记忆在 snapshot/context 装填恒定置顶，上限 5 条/用户。"""
        return await self._request("PUT", f"/api/memory/v1/{memory_id}/pin")

    async def unpin_memory(self, memory_id: str) -> Dict:
        return await self._request("DELETE", f"/api/memory/v1/{memory_id}/pin")

    async def save_history(
        self,
        user_id: str,
        messages: List[Dict],
        app_name: Optional[str] = None,
        session_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> Dict:
        payload: Dict = {
            "user_id": user_id,
            "app_name": app_name or self.app_name,
            "messages": messages,
        }
        if session_id:
            payload["session_id"] = session_id
        if channel:
            payload["channel"] = channel
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
            await self._resolve_base_url()
            client = await self._get_client()
            response = await client.get("/api/health", timeout=3.0)
            if response.status_code == 200:
                data = response.json()
                return data.get("success", False) or data.get("status") == "healthy"
            return False
        except Exception:
            return False


_bm_sdk: Optional[BeeMemorySDK] = None


def get_bm_sdk(app_name: str = "default", timeout: float = 10.0) -> BeeMemorySDK:
    """获取全局共享的 BeeMemorySDK 实例（base_url 惰性解析：显式 > Lion infra > env）.

    app_name 为实例默认值，各方法调用时仍可用 app_name 参数按调用方覆盖。
    """
    global _bm_sdk
    if _bm_sdk is None:
        _bm_sdk = BeeMemorySDK(app_name=app_name, timeout=timeout)
    return _bm_sdk


_ensured_apps: Dict[str, bool] = {}


async def ensure_app_memory(
    app_name: str,
    *,
    similarity_threshold: float,
    expiry_days: int,
    skip_low_value_gate: bool = False,
    timeout: float = 10.0,
) -> bool:
    """beeMemory 应用接入幂等自举：健康检查 + 建应用配置，每进程每 app 一次。

    成功与失败都记忆化且命中返回上次真实结果（失败记忆化=每消息调用形态下
    不重复打健康探针，r152 六仓自举样板单源收归）。服务缺席/配置被拒/异常均
    fail-open 返回 False（调用方读空、写跳过或走本地降级），True=配置就绪。
    """
    if app_name in _ensured_apps:
        return _ensured_apps[app_name]
    sdk = get_bm_sdk(app_name=app_name, timeout=timeout)
    ok = False
    try:
        if await sdk.health_check():
            extra: Dict = {"skip_low_value_gate": True} if skip_low_value_gate else {}
            result = await sdk.ensure_app_config(
                app_name=app_name,
                similarity_threshold=similarity_threshold,
                expiry_days=expiry_days,
                **extra,
            )
            if result.get("success") is False:
                logger.warning("beeMemory 配置确保被拒 (app=%s): %s", app_name, result.get("message"))
            else:
                ok = True
                logger.info("beeMemory config ensured (app=%s)", app_name)
        else:
            logger.warning("beeMemory 服务不可用，记忆面降级 (app=%s)", app_name)
    except Exception as exc:
        logger.warning("beeMemory config ensure failed (app=%s): %s", app_name, exc)
    _ensured_apps[app_name] = ok
    return ok