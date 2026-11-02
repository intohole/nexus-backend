from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nexus import discovery
from nexus.discovery import ServiceRegistry, normalize_name, reset_registry
from nexus import defaults

APPS = [
    {"name": "beeMemory", "port": 8700, "server_tag": "edge-01", "status": "running",
     "path_prefix": "/beeMemory", "is_public": 0},
    {"name": "oneNote", "port": 8550, "server_tag": "edge-03", "status": "running",
     "path_prefix": "/onenote", "is_public": 1},
    {"name": "moutain", "port": 8250, "server_tag": "edge-01", "status": "running",
     "path_prefix": "/spider", "is_public": 0},
    {"name": "chroma-embedding-server", "port": 8999, "server_tag": "edge-04", "status": "running",
     "path_prefix": "/chroma-server", "is_public": 0},
    {"name": "test-app", "port": 0, "server_tag": "edge-01", "status": "created",
     "path_prefix": "", "is_public": 0},
    {"name": "ghostApp", "port": 8099, "server_tag": "edge-99", "status": "running",
     "path_prefix": "", "is_public": 0},
]

NODES = {"nodes": [
    {"tag": "edge-01", "host": "", "is_self": True, "online": True},
    {"tag": "edge-03", "host": "10.100.0.3", "is_self": False, "online": True},
]}


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/apps/":
            return httpx.Response(200, json=APPS)
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json=NODES)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path: Path):
    reset_registry()
    monkeypatch.delenv("MINIDEPLOY_BASE_URL", raising=False)
    monkeypatch.delenv("MINIDEPLOY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("NEXUS_DISCOVERY_NODE_LOCAL", raising=False)
    monkeypatch.setenv("DISCOVERY_CACHE_PATH", str(tmp_path / "snapshot.json"))
    yield
    reset_registry()


def _registry(tmp_path: Path, **kw) -> ServiceRegistry:
    reg = ServiceRegistry(cache_path=tmp_path / "snapshot.json", **kw)
    reg._client = httpx.AsyncClient(
        base_url=reg._base_url, transport=_mock_transport()
    )
    return reg


@pytest.mark.asyncio
async def test_normalize_name_insensitive_with_aliases(tmp_path):
    assert normalize_name("goldenStock") == "golden"
    assert normalize_name("BEE-memory") == "beememory"
    assert normalize_name("spider") == "moutain"
    assert normalize_name("chroma") == "chromaembeddingserver"
    assert normalize_name("chroma-embedding-server") == "chromaembeddingserver"
    reg = _registry(tmp_path)
    await reg.refresh(force=True)
    assert reg.lookup("spider").name == "moutain"
    assert reg.lookup("CHROMA").name == "chroma-embedding-server"
    await reg.close()


@pytest.mark.asyncio
async def test_resolve_internal_and_public(tmp_path):
    reg = _registry(tmp_path)
    assert await reg.resolve("beememory") == "http://10.100.0.1:8700"
    assert await reg.resolve("MOUTAIN") == "http://10.100.0.1:8250"
    assert await reg.resolve("onenote", prefer="public") == "https://songguokr.com/onenote"
    assert await reg.resolve("beememory", prefer="public") == ""
    assert await reg.resolve("no-such-app") == ""
    await reg.close()


@pytest.mark.asyncio
async def test_unknown_node_returns_empty_not_guessed(tmp_path):
    reg = _registry(tmp_path)
    assert await reg.resolve("oneNote") == "http://10.100.0.3:8550"
    assert await reg.resolve("ghostApp") == ""
    await reg.close()


@pytest.mark.asyncio
async def test_bootstrap_fallback_when_registry_empty(tmp_path):
    reg = ServiceRegistry(cache_path=tmp_path / "none.json")
    assert reg.bootstrap("notifycenter") == defaults.DEFAULT_NOTIFY_CENTER_URL
    assert await reg.resolve("beememory") == ""
    await reg.close()


@pytest.mark.asyncio
async def test_master_down_degrades_to_snapshot(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    await reg.refresh(force=True)
    assert reg.state == "ok"
    await reg.close()

    cold = ServiceRegistry(cache_path=tmp_path / "snapshot.json")
    assert cold.state == "cache"
    assert await cold.resolve("beememory") == "http://10.100.0.1:8700"
    cold._client = httpx.AsyncClient(base_url=cold._base_url, transport=httpx.MockTransport(
        lambda req: httpx.Response(503)))
    assert await cold.refresh(force=True) is False
    assert await cold.resolve("beememory") == "http://10.100.0.1:8700"
    await cold.close()


@pytest.mark.asyncio
async def test_ttl_skips_refetch(tmp_path):
    calls = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/api/apps/":
            return httpx.Response(200, json=APPS)
        return httpx.Response(200, json=NODES)

    reg = ServiceRegistry(cache_path=tmp_path / "snapshot.json")
    reg._client = httpx.AsyncClient(
        base_url=reg._base_url, transport=httpx.MockTransport(counting_handler)
    )
    assert await reg.refresh(force=True) is True
    assert calls["n"] == 2
    assert await reg.refresh() is True
    assert calls["n"] == 2
    await reg.close()


@pytest.mark.asyncio
async def test_resolve_service_one_liner(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_CACHE_PATH", str(tmp_path / "snapshot.json"))
    reset_registry()
    reg = discovery.get_registry()
    reg._client = httpx.AsyncClient(base_url=reg._base_url, transport=_mock_transport())
    assert await discovery.resolve_service("beememory") == "http://10.100.0.1:8700"
    assert await discovery.resolve_service("unknown-svc") == ""
    await reg.close()


def test_snapshot_roundtrip(tmp_path):
    payload = {"fetched_at": 1.0, "base_url": "http://10.100.0.1:8900",
               "nodes": [{"tag": "edge-02", "host": "10.100.0.2"}],
               "apps": [{"name": "golden", "port": 8888, "node_tag": "edge-02",
                         "status": "running", "path_prefix": "/golden", "is_public": True}]}
    p = tmp_path / "snapshot.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    reg = ServiceRegistry(cache_path=p)
    assert reg.state == "cache"
    assert reg.lookup("golden-stock").port == 8888
    assert reg.lookup("golden").port == 8888
