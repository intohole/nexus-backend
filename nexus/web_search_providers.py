"""外部搜索引擎 Provider 框架：统一抽象与 zhipu/tavily/serpapi 适配。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Type

import httpx

from nexus.logging import get_logger

logger = get_logger("nexus.web_search_providers")


class ContentSize(str, Enum):
    MEDIUM = "medium"
    HIGH = "high"


class SearchRecency(str, Enum):
    ONE_DAY = "oneDay"
    ONE_WEEK = "oneWeek"
    ONE_MONTH = "oneMonth"
    ONE_YEAR = "oneYear"
    THREE_YEARS = "threeYears"
    FIVE_YEARS = "fiveYears"
    NO_LIMIT = "noLimit"


@dataclass
class SearchConfig:
    engine: str = "search_std"
    max_results: int = 10
    content_size: ContentSize = ContentSize.MEDIUM
    recency: SearchRecency = SearchRecency.NO_LIMIT
    domain_filter: Optional[List[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


@dataclass
class ProviderSearchItem:
    title: str
    content: str = ""
    link: str = ""
    media: str = ""
    icon: str = ""
    refer: str = ""
    publish_date: Optional[str] = None
    score: Optional[float] = None


@dataclass
class ProviderSearchIntent:
    query: str = ""
    intent: str = ""
    keywords: str = ""


@dataclass
class ProviderSearchResponse:
    results: List[ProviderSearchItem] = field(default_factory=list)
    intents: List[ProviderSearchIntent] = field(default_factory=list)
    total_results: int = 0
    from_cache: bool = False
    execution_time_ms: float = 0.0


class SearchProvider(ABC):
    def __init__(self, api_key: Optional[str] = None, base_url: str = "", **kwargs: object):
        self._api_key: Optional[str] = api_key
        self._base_url: str = base_url
        self._client: Optional[httpx.AsyncClient] = None

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def _do_search(self, query: str, config: SearchConfig) -> ProviderSearchResponse: ...

    async def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        if not self._api_key:
            logger.warning(f"{self.name}: API key not configured")
            return False
        self._client = httpx.AsyncClient(timeout=30.0)
        return True

    async def search(self, query: str, config: SearchConfig) -> ProviderSearchResponse:
        if not await self._ensure_client():
            return ProviderSearchResponse()
        try:
            return await self._do_search(query, config)
        except httpx.HTTPStatusError as e:
            logger.error(f"{self.name} HTTP error: {e.response.status_code}")
            return ProviderSearchResponse()
        except Exception as e:
            logger.error(f"{self.name} search failed: {e}")
            return ProviderSearchResponse()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class ZhipuSearchProvider(SearchProvider):
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, **kwargs: object):
        super().__init__(api_key=api_key, base_url=base_url or "https://open.bigmodel.cn/api/paas/v4/")

    @property
    def name(self) -> str:
        return "zhipu"

    async def _do_search(self, query: str, config: SearchConfig) -> ProviderSearchResponse:
        payload = {
            "search_query": query, "search_engine": config.engine,
            "count": config.max_results,
            "content_size": "high" if config.content_size == ContentSize.HIGH else "medium",
        }
        recency_str = _ZHIPU_RECENCY_MAP.get(config.recency)
        if recency_str and recency_str != "noLimit":
            payload["search_recency_filter"] = recency_str
        if config.domain_filter:
            payload["search_domain_filter"] = ",".join(config.domain_filter)

        resp = await self._client.post(
            f"{self._base_url.rstrip('/')}/web_search", json=payload,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        items = [
            ProviderSearchItem(
                title=r.get("title", ""), content=r.get("content", ""),
                link=r.get("link", ""), media=r.get("media", ""),
                icon=r.get("icon", ""), refer=r.get("refer", ""),
                publish_date=r.get("publish_date"),
            )
            for r in data.get("search_result", [])
        ]
        intents = [
            ProviderSearchIntent(query=i.get("query", ""), intent=i.get("intent", ""), keywords=i.get("keywords", ""))
            for i in data.get("search_intent", [])
        ]
        return ProviderSearchResponse(results=items, intents=intents, total_results=len(items))


_ZHIPU_RECENCY_MAP = {
    SearchRecency.ONE_DAY: "oneDay", SearchRecency.ONE_WEEK: "oneWeek",
    SearchRecency.ONE_MONTH: "oneMonth", SearchRecency.ONE_YEAR: "oneYear",
    SearchRecency.THREE_YEARS: "oneYear", SearchRecency.FIVE_YEARS: "oneYear",
    SearchRecency.NO_LIMIT: "noLimit",
}


class TavilySearchProvider(SearchProvider):
    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.tavily.com", **kwargs: object):
        super().__init__(api_key=api_key, base_url=base_url)

    @property
    def name(self) -> str:
        return "tavily"

    async def _do_search(self, query: str, config: SearchConfig) -> ProviderSearchResponse:
        payload = {"query": query, "max_results": config.max_results, "api_key": self._api_key}
        if config.content_size == ContentSize.HIGH:
            payload["search_depth"] = "advanced"
        if config.recency != SearchRecency.NO_LIMIT:
            days_map = {SearchRecency.ONE_DAY: 1, SearchRecency.ONE_WEEK: 7, SearchRecency.ONE_MONTH: 30, SearchRecency.ONE_YEAR: 365}
            days = days_map.get(config.recency)
            if days:
                payload["days"] = days
        if config.domain_filter:
            payload["include_domains"] = config.domain_filter

        resp = await self._client.post(f"{self._base_url}/search", json=payload)
        resp.raise_for_status()
        data = resp.json()

        items = [
            ProviderSearchItem(
                title=r.get("title", ""), content=r.get("content", ""),
                link=r.get("url", ""), media=r.get("source", ""),
                publish_date=r.get("published_date"), score=r.get("score"),
            )
            for r in data.get("results", [])
        ]
        return ProviderSearchResponse(results=items, total_results=len(items))


class SerpAPISearchProvider(SearchProvider):
    def __init__(self, api_key: Optional[str] = None, engine: str = "google", base_url: str = "https://serpapi.com", **kwargs: object):
        super().__init__(api_key=api_key, base_url=base_url)
        self._engine = engine

    @property
    def name(self) -> str:
        return f"serpapi_{self._engine}"

    async def _do_search(self, query: str, config: SearchConfig) -> ProviderSearchResponse:
        params = {"q": query, "num": config.max_results, "api_key": self._api_key, "engine": self._engine}
        if config.recency != SearchRecency.NO_LIMIT:
            tbs_map = {SearchRecency.ONE_DAY: "qdr:d", SearchRecency.ONE_WEEK: "qdr:w", SearchRecency.ONE_MONTH: "qdr:m", SearchRecency.ONE_YEAR: "qdr:y"}
            tbs = tbs_map.get(config.recency)
            if tbs:
                params["tbs"] = tbs
        if config.domain_filter:
            params["sites"] = ",".join(config.domain_filter)

        resp = await self._client.get(f"{self._base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        items = [
            ProviderSearchItem(
                title=r.get("title", ""), content=r.get("snippet", ""),
                link=r.get("link", ""), media=r.get("source", ""),
                publish_date=r.get("date"), score=r.get("position"),
            )
            for r in data.get("organic_results", [])
        ]
        return ProviderSearchResponse(results=items, total_results=len(items))


_registry: dict[str, Type[SearchProvider]] = {}


def register_provider(name: str, provider_class: Type[SearchProvider]) -> None:
    _registry[name] = provider_class
    logger.debug(f"Registered search provider: {name}")


def get_provider(name: str, **kwargs: object) -> Optional[SearchProvider]:
    provider_class = _registry.get(name)
    if provider_class is None:
        logger.warning(f"Unknown search provider: {name}, available: {list(_registry.keys())}")
        return None
    return provider_class(**kwargs)


def list_providers() -> list[str]:
    return list(_registry.keys())


def auto_register_providers() -> None:
    register_provider("zhipu", ZhipuSearchProvider)
    register_provider("gateway", ZhipuSearchProvider)
    register_provider("tavily", TavilySearchProvider)
    register_provider("serpapi", SerpAPISearchProvider)


auto_register_providers()

__all__ = [
    "ContentSize",
    "SearchRecency",
    "SearchConfig",
    "ProviderSearchItem",
    "ProviderSearchIntent",
    "ProviderSearchResponse",
    "SearchProvider",
    "ZhipuSearchProvider",
    "TavilySearchProvider",
    "SerpAPISearchProvider",
    "register_provider",
    "get_provider",
    "list_providers",
    "auto_register_providers",
]
