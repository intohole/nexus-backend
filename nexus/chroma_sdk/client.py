from __future__ import annotations

import json
import os

import httpx


class ChromaSDK:

    def __init__(
        self,
        base_url: str = "",
        service_token: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            base_url = os.environ.get("CHROMA_BASE_URL", "${CHROMA_BASE_URL}")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token or os.environ.get("SERVICE_TOKEN")
        self._api_key = api_key or os.environ.get("CHROMA_API_KEY")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._service_token:
                headers["X-Service-Token"] = self._service_token
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ChromaSDK:
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
        method: str,
        path: str,
        json_data: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        client = await self._get_client()
        try:
            response = await client.request(method, path, json=json_data, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            detail = e.response.text
            try:
                body = e.response.json()
                detail = body.get("detail", detail)
            except (json.JSONDecodeError, ValueError):
                pass
            return {"success": False, "detail": f"HTTP {e.response.status_code}: {detail}"}
        except httpx.ConnectError:
            return {"success": False, "detail": f"Cannot connect to Chroma at {self._base_url}"}
        except httpx.TimeoutException:
            return {"success": False, "detail": f"Chroma request timeout at {self._base_url}"}
        except httpx.RequestError as e:
            return {"success": False, "detail": f"Chroma request error: {e}"}
        except (json.JSONDecodeError, ValueError):
            return {"success": False, "detail": "Chroma response parse error"}

    def _is_error(self, result: dict[str, object]) -> bool:
        return result.get("success") is False

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