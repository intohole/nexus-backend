"""审计日志客户端：异步上报操作留痕至审计服务。"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urljoin

import httpx

from nexus.defaults import DEFAULT_UC_BASE_URL
from nexus.logging import get_logger
from nexus.service_client import get_service_token

logger = get_logger("nexus.audit")


def _base_url() -> str:
    return os.getenv("UC_BASE_URL", DEFAULT_UC_BASE_URL).rstrip("/")


async def log_audit(
    action: str,
    user_id: int | None = None,
    app_code: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    status_code: int | None = None,
) -> bool:
    """上报业务审计到 usercenter（失败仅记日志，不影响主流程）。

    管理操作低频，每次走短连接，免维护模块级 client 的关闭路径。
    """
    token = await get_service_token()
    if not token:
        logger.debug("service token 未配置, 跳过审计上报 action=%s", action)
        return False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            response = await client.post(
                urljoin(_base_url(), "/api/internal/audit/record"),
                headers={"X-Service-Token": token},
                json={
                    "action": action,
                    "user_id": user_id,
                    "app_code": app_code,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "detail": detail,
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                    "status_code": status_code,
                },
            )
            return response.status_code < 400
    except Exception as exc:
        logger.warning("审计上报失败 action=%s: %s", action, exc)
        return False
