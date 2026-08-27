"""Moutain 望岳服务客户端: 统一封装智能内容监测的搜索/爬取/RSS能力."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from nexus.infra import get_moutain_config
from nexus.logging import get_logger

logger = get_logger("nexus.moutain")


class MoutainClient:
    """望岳(智能内容监测)服务客户端.

    提供异步任务提交(带回调)与直接同步调用两种模式。
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._configured_base_url: str = ""
        self._configured_service_token: str = ""

    async def _ensure_client(self) -> httpx.AsyncClient:
        cfg = await get_moutain_config()
        base_url = (cfg.get("base_url") or "").rstrip("/")
        service_token = cfg.get("service_token") or ""
        if (
            self._client is not None
            and not self._client.is_closed
            and self._configured_base_url == base_url
            and self._configured_service_token == service_token
        ):
            return self._client
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if service_token:
            headers["X-Service-Token"] = service_token
        self._client = httpx.AsyncClient(timeout=30.0, headers=headers)
        self._configured_base_url = base_url
        self._configured_service_token = service_token
        return self._client

    async def _post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        client = await self._ensure_client()
        try:
            resp = await client.post(f"{self._configured_base_url}{path}", json=payload)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Moutain API %s HTTP error: %s, body=%s",
                path,
                exc.response.status_code,
                exc.response.text[:200],
            )
        except Exception as exc:
            logger.error("Moutain API %s error: %s: %s", path, type(exc).__name__, exc)
        return None

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self._configured_base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Moutain API GET %s HTTP error: %s, body=%s",
                path,
                exc.response.status_code,
                exc.response.text[:200],
            )
        except Exception as exc:
            logger.error("Moutain API GET %s error: %s: %s", path, type(exc).__name__, exc)
        return None

    # -- 异步任务提交模式(带回调) --

    async def submit_crawl_task(
        self,
        url: str,
        callback_url: str,
        source: str = "site",
        method: str = "GET",
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        form_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        response_type: str = "json",
        render: bool = False,
        csrf_pre_request: Optional[Dict[str, Any]] = None,
        callback_headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """提交爬取任务(异步回调模式)."""
        payload = {
            "url": url,
            "method": method,
            "params": params,
            "json_body": json_body,
            "form_data": form_data,
            "headers": headers,
            "response_type": response_type,
            "render": render,
            "csrf_pre_request": csrf_pre_request,
            "callback_url": callback_url,
            "callback_headers": callback_headers,
            "source": source,
            "timeout": timeout,
        }
        return await self._post("/api/v1/crawler/execute", payload)

    async def submit_websearch_task(
        self,
        query: str,
        callback_url: str,
        source: str = "websearch",
        count: int = 10,
        search_engine: str = "search_std",
        content_size: str = "medium",
        callback_headers: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """提交网页搜索任务(异步回调模式)."""
        payload = {
            "query": query,
            "count": count,
            "search_engine": search_engine,
            "content_size": content_size,
            "callback_url": callback_url,
            "callback_headers": callback_headers,
            "source": source,
        }
        return await self._post("/api/v1/crawler/websearch", payload)

    async def submit_rss_task(
        self,
        url: str,
        callback_url: str,
        source: str = "rss",
        callback_headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """提交RSS获取任务(异步回调模式)."""
        payload = {
            "url": url,
            "callback_url": callback_url,
            "callback_headers": callback_headers,
            "source": source,
            "timeout": timeout,
        }
        return await self._post("/api/v1/crawler/fetch-rss", payload)

    async def submit_batch_websearch(
        self,
        queries: List[str],
        callback_url: str,
        source: str = "websearch",
        count: int = 10,
        callback_headers: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """批量提交网页搜索任务."""
        results: List[Dict[str, Any]] = []
        for query in queries:
            result = await self.submit_websearch_task(
                query=query,
                callback_url=callback_url,
                source=source,
                count=count,
                callback_headers=callback_headers,
            )
            if result:
                results.append(result)
        return results

    async def submit_batch_rss(
        self,
        urls: List[str],
        callback_url: str,
        source: str = "rss",
        callback_headers: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """批量提交RSS获取任务."""
        results: List[Dict[str, Any]] = []
        for url in urls:
            result = await self.submit_rss_task(
                url=url,
                callback_url=callback_url,
                source=source,
                callback_headers=callback_headers,
            )
            if result:
                results.append(result)
        return results

    # -- 直接调用模式(同步返回) --

    async def search(self, query: str, count: int = 6) -> List[Dict[str, Any]]:
        """直接搜索，返回搜索结果列表."""
        payload = {"query": query, "count": count, "enable_external_search": True}
        data = await self._post("/api/websearch/search", payload)
        if not data:
            return []
        if isinstance(data, dict) and data.get("code") == 401:
            return []
        result = data.get("search_result", []) if isinstance(data, dict) else []
        if not result and isinstance(data, dict) and isinstance(data.get("data"), dict):
            result = data["data"].get("search_result", [])
        return result

    async def crawl(self, url: str) -> Optional[Dict[str, Any]]:
        """直接爬取指定URL，返回页面内容."""
        data = await self._post("/api/spider/crawl", {"url": url})
        if not data:
            return None
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(payload, dict):
            metadata = payload.get("metadata") or {}
            return {
                "title": metadata.get("title", "") or payload.get("title", ""),
                "content": payload.get("content", ""),
                "url": url,
            }
        return None

    async def hot_news(self, page: int = 1, per_page: int = 20) -> Optional[Dict[str, Any]]:
        """获取热点新闻列表."""
        return await self._get("/api/topic/hot_news", {"page": page, "per_page": per_page})

    async def deep_search(self, query: str, max_results: int = 10) -> Optional[Dict[str, Any]]:
        """深度搜索，聚合多源信息返回结构化结果."""
        return await self._post("/api/deepsearch", {"query": query, "max_results": max_results})

    async def close(self) -> None:
        """关闭底层HTTP客户端."""
        if self._client:
            await self._client.aclose()
            self._client = None


_moutain_client: Optional[MoutainClient] = None


def get_moutain_client() -> MoutainClient:
    """获取全局共享的MoutainClient实例."""
    global _moutain_client
    if _moutain_client is None:
        _moutain_client = MoutainClient()
    return _moutain_client