from __future__ import annotations

from typing import Any, Optional

from nexus.infra import get_spider_config
from nexus.logging import get_logger

logger = get_logger("nexus.web_search")

VALID_RECENCY_FILTERS = (
    "noLimit",
    "oneDay",
    "oneWeek",
    "oneMonth",
    "oneYear",
    "threeYears",
    "fiveYears",
)


class WebSearchService:
    _instance: Optional["WebSearchService"] = None
    _tool: Optional[object] = None
    _configured_base_url: str = ""
    _configured_token: str = ""

    def __new__(cls) -> "WebSearchService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def _ensure_tool(self) -> object:
        cfg = await get_spider_config()
        base_url = cfg.get("base_url", "http://localhost:8250")
        token = cfg.get("service_token", "")
        if self._tool is not None and self._configured_base_url == base_url and self._configured_token == token:
            return self._tool
        from ironman.tools.websearch_tool import WebSearchTool

        self._tool = WebSearchTool(
            spider_base_url=base_url,
            service_token=token,
            timeout=30.0,
        )
        self._configured_base_url = base_url
        self._configured_token = token
        return self._tool

    async def search(
        self,
        query: str,
        count: int = 5,
        recency: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if not query or not query.strip():
            return []
        tool = await self._ensure_tool()
        try:
            return await tool.search_raw(query, count, recency=recency)
        except Exception as e:
            logger.error("Web search failed (query=%s): %s", query[:50], e)
            return []

    async def search_with_context(
        self,
        query: str,
        count: int = 5,
        recency: Optional[str] = None,
    ) -> str:
        results = await self.search(query, count, recency=recency)
        if not results:
            return ""
        parts: list[str] = []
        for i, r in enumerate(results, 1):
            title = str(r.get("title", ""))
            content = str(r.get("content", ""))
            media = str(r.get("media", ""))
            link = str(r.get("link", ""))
            if title and content:
                source = f" ({media})" if media else ""
                parts.append(f"{i}. {title}{source}\n   {content}")
                if link:
                    parts.append(f"   来源: {link}")
        return "\n".join(parts)

    async def close(self) -> None:
        if self._tool is not None:
            try:
                await self._tool.close()
            except Exception:
                pass
            self._tool = None


_web_search_service: Optional[WebSearchService] = None


def get_web_search_service() -> WebSearchService:
    global _web_search_service
    if _web_search_service is None:
        _web_search_service = WebSearchService()
    return _web_search_service
