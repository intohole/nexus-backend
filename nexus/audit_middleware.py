"""审计中间件：按请求采集操作留痕并异步上报。"""
from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable, Optional, Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nexus.audit import log_audit
from nexus.config import NexusConfig, get_settings
from nexus.logging import get_logger
from nexus.utils import get_client_ip

logger = get_logger("nexus.audit_middleware")


class AuditMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config: Optional[NexusConfig] = None) -> None:
        super().__init__(app)
        cfg: NexusConfig = config or get_settings()
        audit_cfg = cfg.audit
        self._enabled: bool = audit_cfg.enabled
        self._exclude_paths: list[str] = audit_cfg.exclude_paths
        self._app_code: Optional[str] = None
        for source in (audit_cfg.app_code, cfg.uc.app_key, os.getenv("LION_NAMESPACE")):
            if source:
                self._app_code = str(source)
                break
        self._background_tasks: Set[asyncio.Task] = set()

    def _is_excluded(self, path: str) -> bool:
        for exclude in self._exclude_paths:
            if path == exclude or path.startswith(exclude + "/") or path.startswith(exclude):
                return True
        return False

    def _extract_user_id(self, request: Request) -> Optional[int]:
        state_user = getattr(request.state, "user", None)
        if state_user is not None:
            uid = getattr(state_user, "id", None)
            if uid is None and isinstance(state_user, dict):
                uid = state_user.get("id")
            if uid is not None:
                try:
                    return int(uid)
                except (TypeError, ValueError):
                    return None
        raw = request.headers.get("X-User-Id")
        if raw:
            try:
                return int(raw)
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_resource(path: str) -> tuple[Optional[str], Optional[str]]:
        parts: list[str] = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return None, None
        resource_type: str = parts[0]
        resource_id: Optional[str] = parts[-1] if len(parts) > 1 else None
        return resource_type, resource_id

    def _schedule(self, method: str, path: str, query: str, user_id: Optional[int],
                  ip_address: str, user_agent: str, status_code: int) -> None:
        resource_type, resource_id = self._parse_resource(path)
        detail: dict[str, object] = {"method": method, "path": path}
        if query:
            detail["query"] = query
        task = asyncio.create_task(
            log_audit(
                action="request",
                user_id=user_id,
                app_code=self._app_code,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                ip_address=ip_address,
                user_agent=user_agent,
                status_code=status_code,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not self._enabled:
            return await call_next(request)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        path: str = request.url.path
        if self._is_excluded(path):
            return await call_next(request)
        response: Response = await call_next(request)
        self._schedule(
            method=request.method,
            path=path,
            query=request.url.query,
            user_id=self._extract_user_id(request),
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("User-Agent", ""),
            status_code=response.status_code,
        )
        return response