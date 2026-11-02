"""服务地址发现：miniDeploy 注册表单权威，全舰队统一解析原语。

地址拓扑的权威源 = miniDeploy 应用注册表（port + server_tag 随部署唯一落库），
本模块是其集群内唯一读侧：TTL 缓存 + 磁盘快照降级 + defaults 引导兜底表。
master 不可达时按「快照 → 引导表」降级续命，永不 raise。

解析链（地址族完整顺序，见 handoff/17「服务端点解析契约」）：
  注册表 → env 显式覆盖 → Lion infra 命名空间（遗留） → defaults 引导表
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from nexus.defaults import BOOTSTRAP_SERVICE_URLS, DEFAULT_MINIDEPLOY_BASE_URL
from nexus.logging import get_logger

logger = get_logger("nexus.discovery")

_DEFAULT_TTL_S: float = 300.0
_FAILURE_BACKOFF_S: float = 60.0
_REQUEST_TIMEOUT: float = httpx.Timeout(5.0, connect=2.0)

# 语义名 → 注册表应用名的别名（应用更名/多身份历史遗留，生产注册表 2026-11-03 对表）
_SERVICE_ALIASES: dict[str, str] = {
    "spider": "moutain",
    "chroma": "chroma-embedding-server",
    "goldenstock": "golden",
    "promptgenius": "prompt",
}


def normalize_name(name: str) -> str:
    """服务名归一：大小写与 -/_ 连字符不敏感（goldenStock/golden-stock/golden_stock 同键）。"""
    cleaned = name.strip().lower().replace("-", "").replace("_", "")
    resolved = _SERVICE_ALIASES.get(cleaned)
    return normalize_name(resolved) if resolved else cleaned


@dataclass
class ServiceEndpoint:
    name: str
    port: int
    node_tag: str = ""
    status: str = ""
    path_prefix: str = ""
    is_public: bool = False


class ServiceRegistry:
    def __init__(
        self,
        base_url: str = "",
        service_token: str = "",
        cache_path: str | Path = "",
        ttl_s: float = 0.0,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.getenv("MINIDEPLOY_BASE_URL", "").rstrip("/")
            or DEFAULT_MINIDEPLOY_BASE_URL
        )
        self._service_token: str = service_token or (
            os.getenv("MINIDEPLOY_SERVICE_TOKEN", "")
            or os.getenv("SERVICE_TOKEN", "")
        )
        self._ttl_s: float = ttl_s or float(
            os.getenv("DISCOVERY_TTL_S", "") or _DEFAULT_TTL_S
        )
        self._cache_path: Path = Path(
            cache_path
            or os.getenv("DISCOVERY_CACHE_PATH", "")
            or Path.home() / ".cache" / "nexus_service_registry.json"
        )
        self._endpoints: dict[str, ServiceEndpoint] = {}
        self._node_hosts: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._last_attempt: float = 0.0
        self._state: str = "cold"
        self._lock: asyncio.Lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._own_tag_cached: str = ""
        self._load_snapshot()

    @property
    def state(self) -> str:
        return self._state

    @property
    def snapshot_age_s(self) -> float:
        return max(0.0, time.time() - self._fetched_at) if self._fetched_at else -1.0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self._service_token:
                headers["X-Service-Token"] = self._service_token
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=_REQUEST_TIMEOUT, headers=headers
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def refresh(self, force: bool = False) -> bool:
        """从 master 拉取注册表并重建节点映射；任何失败静默保持现状返回 False。

        失败后按 _FAILURE_BACKOFF_S 退避重试，避免 master 不可达时每次解析都吃连接超时。
        """
        if not force and self._state == "ok" and self.snapshot_age_s < self._ttl_s:
            return True
        if not force and time.time() - self._last_attempt < _FAILURE_BACKOFF_S:
            return self._state == "ok"
        async with self._lock:
            if not force and self._state == "ok" and self.snapshot_age_s < self._ttl_s:
                return True
            if not force and time.time() - self._last_attempt < _FAILURE_BACKOFF_S:
                return self._state == "ok"
            self._last_attempt = time.time()
            try:
                client = await self._get_client()
                apps_resp = await client.get("/api/apps/")
                apps_resp.raise_for_status()
                nodes_resp = await client.get("/api/nodes")
                nodes_resp.raise_for_status()
                apps: list[dict] = apps_resp.json()
                nodes: list[dict] = (nodes_resp.json() or {}).get("nodes", [])
                self._apply(apps, nodes)
                self._fetched_at = time.time()
                self._state = "ok"
                self._save_snapshot()
                return True
            except Exception as exc:
                if self._state == "ok":
                    logger.warning("服务注册表拉取失败，降级缓存/引导表: %s", exc)
                    self._state = "cache" if self._endpoints else "cold"
                return False

    def _apply(self, apps: list[dict], nodes: list[dict]) -> None:
        master_host = urlparse(self._base_url).hostname or ""
        node_hosts: dict[str, str] = {}
        for node in nodes:
            tag = str(node.get("tag") or "")
            host = str(node.get("host") or "")
            if node.get("is_self") and not host:
                host = master_host
            if tag and host:
                node_hosts[tag] = host
        endpoints: dict[str, ServiceEndpoint] = {}
        for app in sorted(apps, key=lambda a: (str(a.get("name") or ""), int(a.get("port") or 0))):
            name = str(app.get("name") or "").strip()
            port = int(app.get("port") or 0)
            if not name or port <= 0:
                continue
            key = normalize_name(name)
            if key in endpoints:
                logger.warning("注册表重名应用 %s，后到覆盖（%s）", name, key)
            endpoints[key] = ServiceEndpoint(
                name=name,
                port=port,
                node_tag=str(app.get("server_tag") or app.get("node_tag") or ""),
                status=str(app.get("status") or ""),
                path_prefix=str(app.get("path_prefix") or ""),
                is_public=bool(app.get("is_public")),
            )
        self._endpoints = endpoints
        self._node_hosts = node_hosts

    def _host_for(self, endpoint: ServiceEndpoint) -> str:
        return self._node_hosts.get(endpoint.node_tag, "")

    def lookup(self, name: str) -> ServiceEndpoint | None:
        return self._endpoints.get(normalize_name(name))

    async def resolve(self, name: str, prefer: str = "internal") -> str:
        """解析服务基址；未知名/不可用态返回空串，调用方自行走下一级兜底。"""
        await self.refresh()
        endpoint = self.lookup(name)
        if endpoint is None:
            return ""
        if prefer == "public":
            if endpoint.is_public and endpoint.path_prefix:
                origin = os.getenv("PUBLIC_WEB_ORIGIN", "https://songguokr.com")
                return origin.rstrip("/") + endpoint.path_prefix
            return ""
        host = self._host_for(endpoint)
        if not host:
            return ""
        if os.getenv("NEXUS_DISCOVERY_NODE_LOCAL", "") == "1" and self._own_tag() == endpoint.node_tag:
            host = "127.0.0.1"
        return f"http://{host}:{endpoint.port}"

    def bootstrap(self, name: str) -> str:
        return BOOTSTRAP_SERVICE_URLS.get(normalize_name(name), "")

    def _own_tag(self) -> str:
        cached = getattr(self, "_own_tag_cached", "")
        if cached:
            return cached
        tag = os.getenv("MINIDEPLOY_SERVER_TAG", "")
        if not tag:
            try:
                import yaml

                raw = yaml.safe_load(Path("minideploy.yaml").read_text(encoding="utf-8"))
                tag = str((raw or {}).get("server_tag") or "")
            except Exception:
                tag = ""
        self._own_tag_cached = tag
        return tag

    def _save_snapshot(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "fetched_at": self._fetched_at,
                "base_url": self._base_url,
                "nodes": [{"tag": t, "host": h} for t, h in self._node_hosts.items()],
                "apps": [
                    {
                        "name": e.name,
                        "port": e.port,
                        "node_tag": e.node_tag,
                        "status": e.status,
                        "path_prefix": e.path_prefix,
                        "is_public": e.is_public,
                    }
                    for e in self._endpoints.values()
                ],
            }
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_path)
        except Exception as exc:
            logger.debug("注册表快照落盘失败（不影响解析）: %s", exc)

    def _load_snapshot(self) -> None:
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._apply(raw.get("apps", []), [
                {"tag": n["tag"], "host": n["host"], "is_self": False}
                for n in raw.get("nodes", [])
            ])
            self._fetched_at = float(raw.get("fetched_at") or 0.0)
            self._state = "cache" if self._endpoints else "cold"
        except Exception:
            self._state = "cold"

    def snapshot_info(self) -> dict[str, object]:
        return {
            "state": self._state,
            "age_s": round(self.snapshot_age_s, 1),
            "apps": len(self._endpoints),
            "nodes": len(self._node_hosts),
            "base_url": self._base_url,
        }


_registry: ServiceRegistry | None = None


def get_registry() -> ServiceRegistry:
    global _registry
    if _registry is None:
        _registry = ServiceRegistry()
    return _registry


async def resolve_service(name: str, prefer: str = "internal") -> str:
    """一行式解析：注册表未知名时回落 defaults 引导表。"""
    registry = get_registry()
    url = await registry.resolve(name, prefer=prefer)
    if url:
        return url
    return registry.bootstrap(name)


def reset_registry() -> None:
    """测试隔离用：丢弃单例与缓存态。"""
    global _registry
    _registry = None
