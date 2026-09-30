"""限流键解析：XFF 防伪造的客户端 IP 提取与 key_func 统一兜底。"""
from __future__ import annotations

import ipaddress
from typing import Callable, Optional

from starlette.requests import Request

from nexus.logging import get_logger

_logger = get_logger("nexus.rate_limit")


def client_ip(request: Request) -> str:
    """限流键用客户端 IP：取 XFF 最右侧公网地址。

    与 nexus.utils.net.get_client_ip（取 XFF 首项，供日志/审计展示、客户端可伪造）
    语义不同，勿合并：本实现取最右侧公网项——该值由自有代理链追加，伪造不影响计桶。
    """
    forwarded: Optional[str] = request.headers.get("X-Forwarded-For")
    if forwarded:
        ips: list[str] = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        for ip in reversed(ips):
            try:
                parsed = ipaddress.ip_address(ip)
                if not parsed.is_private and not parsed.is_loopback:
                    return ip
            except ValueError:
                continue
        if ips:
            return ips[-1]
    return request.client.host if request.client else "unknown"


def resolve_client_id(request: Request, key_func: Optional[Callable[[Request], str]] = None) -> str:
    """限流维度键：key_func 优先（支持 ip:app 等复合键），异常或缺省回退 client_ip。"""
    if key_func is None:
        return client_ip(request)
    try:
        return key_func(request)
    except Exception as exc:
        _logger.warning("rate limit key_func failed: %s", exc)
        return "unknown"
