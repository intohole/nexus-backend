"""PromptManager 网关型服务的公共基座：单例守卫 + 惰性配置解析 + Bearer POST。"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 90.0


class _GatewayServiceBase:
    REQUEST_TIMEOUT = REQUEST_TIMEOUT

    def __new__(cls) -> "_GatewayServiceBase":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._base_url = ""
        self._api_key = ""
        self._model = ""
        self._lock = asyncio.Lock()
        self._resolved = False
        self._initialized = True

    async def _resolve_config(self, config_key: str, default_model: str | None = None) -> None:
        if self._resolved:
            return
        async with self._lock:
            if self._resolved:
                return
            from nexus.llm_config import resolve_gateway_endpoint

            (
                self._base_url,
                self._api_key,
                self._model,
            ) = await resolve_gateway_endpoint(config_key, default_model)
            self._resolved = True
            logger.info(
                "%s resolved: base_url=%s model=%s",
                type(self).__name__,
                self._base_url,
                self._model,
            )

    def _require_config(self, capability: str) -> None:
        if not self._base_url or not self._api_key:
            raise RuntimeError(f"{capability} gateway config missing (base_url/api_key)")

    async def _post(self, url: str, payload: dict[str, object], op: str) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"{op} failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()
