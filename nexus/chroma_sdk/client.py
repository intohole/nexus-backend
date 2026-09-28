"""chroma 向量库 SDK 客户端：集合与向量读写封装。"""
from __future__ import annotations

import os

from nexus.sdk_base import BaseAsyncClient
from nexus.service_client import get_service_token


class ChromaSDK(BaseAsyncClient):
    service_name = "Chroma"

    def __init__(
        self,
        base_url: str = "",
        service_token: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            base_url = os.environ.get("CHROMA_BASE_URL", "${CHROMA_BASE_URL}")
        super().__init__(base_url, timeout=timeout)
        self._service_token = service_token
        self._api_key = api_key or os.environ.get("CHROMA_API_KEY")

    async def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token: str = self._service_token or await get_service_token()
        if token:
            headers["X-Service-Token"] = token
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return await super()._request(method, path, json_data=json_data, params=params)

    async def health(self) -> dict[str, object]:
        return await self._request("GET", "/health")

    async def stats(self) -> dict[str, object]:
        return await self._request("GET", "/stats")

    async def list_collections(self) -> list[dict[str, object]]:
        result = await self._request("GET", "/api/v1/collections")
        if self._is_error(result):
            return []
        collections = result.get("collections")
        if isinstance(collections, list):
            return collections
        return []

    async def create_collection(
        self,
        name: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"name": name}
        if metadata is not None:
            payload["metadata"] = metadata
        return await self._request("POST", "/api/v1/collections", json_data=payload)

    async def get_collection(self, name: str) -> dict[str, object]:
        return await self._request("GET", f"/api/v1/collections/{name}")

    async def ensure_collection(
        self,
        name: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        result = await self.get_collection(name)
        if isinstance(result, dict) and result.get("success") is False and "404" in str(result.get("detail", "")):
            return await self.create_collection(name, metadata)
        return result

    async def delete_collection(self, name: str) -> dict[str, object]:
        return await self._request("DELETE", f"/api/v1/collections/{name}")

    async def add_documents(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"ids": ids}
        if documents is not None:
            payload["documents"] = documents
        if embeddings is not None:
            payload["embeddings"] = embeddings
        if metadatas is not None:
            payload["metadatas"] = metadatas
        return await self._request(
            "POST", f"/api/v1/collections/{collection_name}/documents", json_data=payload,
        )

    async def query_documents(
        self,
        collection_name: str,
        query_texts: list[str] | None = None,
        query_embeddings: list[list[float]] | None = None,
        n_results: int = 10,
        where: dict[str, object] | None = None,
        include: list[str] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"n_results": n_results}
        if query_texts is not None:
            payload["query_texts"] = query_texts
        if query_embeddings is not None:
            payload["query_embeddings"] = query_embeddings
        if where is not None:
            payload["where"] = where
        if include is not None:
            payload["include"] = include
        return await self._request(
            "POST", f"/api/v1/collections/{collection_name}/documents/query", json_data=payload,
        )

    async def update_documents(
        self,
        collection_name: str,
        ids: list[str],
        documents: list[str] | None = None,
        embeddings: list[list[float]] | None = None,
        metadatas: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"ids": ids}
        if documents is not None:
            payload["documents"] = documents
        if embeddings is not None:
            payload["embeddings"] = embeddings
        if metadatas is not None:
            payload["metadatas"] = metadatas
        return await self._request(
            "PUT", f"/api/v1/collections/{collection_name}/documents", json_data=payload,
        )

    async def delete_documents(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        if ids is not None:
            payload["ids"] = ids
        if where is not None:
            payload["where"] = where
        return await self._request(
            "DELETE", f"/api/v1/collections/{collection_name}/documents", json_data=payload,
        )

    async def get_documents(
        self,
        collection_name: str,
        ids: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
    ) -> dict[str, object]:
        params: dict[str, object] = {}
        if ids is not None:
            params["ids"] = ",".join(ids)
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        if include is not None:
            params["include"] = ",".join(include)
        return await self._request(
            "GET", f"/api/v1/collections/{collection_name}/documents", params=params,
        )