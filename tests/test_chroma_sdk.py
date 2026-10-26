"""ChromaSDK 契约测试：not-found 判定口径 / ensure_collection 幂等 / upsert 端点 / 寻址兜底。"""

import asyncio

from nexus.chroma_sdk import ChromaSDK


def _make_sdk(monkeypatch, responses):
    sdk = ChromaSDK(base_url="http://chroma.test:8999")
    calls: list[tuple] = []

    async def fake_request(method, path, json_data=None, params=None):
        calls.append((method, path, json_data, params))
        key = f"{method} {path}"
        for pattern, result in responses:
            if pattern(key):
                if isinstance(result, Exception):
                    raise result
                return result
        return {"success": True}

    monkeypatch.setattr(sdk, "_request", fake_request)
    return sdk, calls


def test_is_not_found_three_signals():
    assert ChromaSDK.is_not_found({"success": False, "detail": "HTTP 404: Collection [x] does not exist"})
    assert ChromaSDK.is_not_found({"success": False, "detail": "Collection not found"})
    assert ChromaSDK.is_not_found({"success": False, "detail": "collection xyz does not exist"})
    assert not ChromaSDK.is_not_found({"success": False, "detail": "HTTP 400: dimension mismatch"})
    assert not ChromaSDK.is_not_found({"name": "x", "count": 0})


def test_ensure_collection_creates_on_missing(monkeypatch):
    sdk, calls = _make_sdk(monkeypatch, [
        (lambda k: k.startswith("GET /api/v1/collections/col"), {"success": False, "detail": "HTTP 404: Collection [col] does not exist"}),
        (lambda k: k.startswith("POST /api/v1/collections"), {"name": "col", "count": 0}),
    ])
    result = asyncio.run(sdk.ensure_collection("col", metadata={"hnsw:space": "cosine"}))
    assert result == {"name": "col", "count": 0}
    assert calls[0][0] == "GET" and calls[1][0] == "POST"
    assert calls[1][2] == {"name": "col", "metadata": {"hnsw:space": "cosine"}}


def test_ensure_collection_returns_existing_without_create(monkeypatch):
    sdk, calls = _make_sdk(monkeypatch, [
        (lambda k: k.startswith("GET /api/v1/collections/col"), {"name": "col", "count": 5}),
    ])
    result = asyncio.run(sdk.ensure_collection("col"))
    assert result == {"name": "col", "count": 5}
    assert len(calls) == 1


def test_ensure_collection_other_error_not_swallowed(monkeypatch):
    sdk, calls = _make_sdk(monkeypatch, [
        (lambda k: k.startswith("GET /api/v1/collections/col"), {"success": False, "detail": "HTTP 500: boom"}),
    ])
    result = asyncio.run(sdk.ensure_collection("col"))
    assert result == {"success": False, "detail": "HTTP 500: boom"}
    assert len(calls) == 1


def test_upsert_documents_posts_upsert_endpoint(monkeypatch):
    sdk, calls = _make_sdk(monkeypatch, [
        (lambda k: k == "POST /api/v1/collections/col/documents/upsert", {"collection": "col", "upserted": 2}),
    ])
    result = asyncio.run(sdk.upsert_documents(
        "col", ids=["a", "b"], embeddings=[[0.1], [0.2]], documents=["x", "y"],
        metadatas=[{"k": 1}, {"k": 2}],
    ))
    assert result == {"collection": "col", "upserted": 2}
    method, path, json_data, _ = calls[0]
    assert (method, path) == ("POST", "/api/v1/collections/col/documents/upsert")
    assert json_data == {"ids": ["a", "b"], "embeddings": [[0.1], [0.2]], "documents": ["x", "y"], "metadatas": [{"k": 1}, {"k": 2}]}


def test_base_url_default_and_env(monkeypatch):
    monkeypatch.delenv("CHROMA_BASE_URL", raising=False)
    sdk = ChromaSDK()
    assert sdk.base_url == "http://localhost:8999"
    monkeypatch.setenv("CHROMA_BASE_URL", "http://edge-04:8999")
    sdk = ChromaSDK()
    assert sdk.base_url == "http://edge-04:8999"
    sdk = ChromaSDK(base_url="http://explicit:1/")
    assert sdk.base_url == "http://explicit:1"
