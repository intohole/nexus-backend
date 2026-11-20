"""限流一键装配：跨仓 setup_rate_limit 克隆族（六仓 38-48 行同构）的单源收归面。

仓侧只留 RULES 策略数据与一行 assemble_rate_limit(app, RULES)；身份键层次
（凭证哈希→匿名 client_ip）、豁免默认面与全局兜底档位在库单源演进。
"""
from __future__ import annotations

from functools import partial
from typing import Callable, Optional, Sequence

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import Response

from nexus.rate_limit.keys import credential_identity
from nexus.rate_limit.middleware import LimitInfo, PathRule, RateLimitMiddleware

DEFAULT_EXEMPT_PATHS: tuple[str, ...] = (
    "/health", "/static", "/favicon", "/docs", "/openapi.json",
)


def assemble_rate_limit(
    app: FastAPI,
    route_rules: Optional[Sequence[PathRule]] = None,
    *,
    global_rpm: int = 120,
    global_rph: int = 0,
    exempt_paths: Sequence[str] = DEFAULT_EXEMPT_PATHS,
    identity: str = "credential",
    credential_headers: Sequence[str] = ("Authorization",),
    anon_header: Optional[str] = None,
    limit_response: Optional[Callable[[Request, LimitInfo], Response]] = None,
) -> None:
    """限流一键装配：route_rules 路径前缀严档（可缺省=纯全局档）+全局兜底。

    identity="credential"：凭证哈希键（credential_headers 链哈希→anon_header→client_ip 回退）；
    identity="client_ip"：纯 IP 键（key_func 缺省形态，financialKG 族）。
    """
    if identity == "credential":
        key_func: Optional[Callable[[Request], str]] = partial(
            credential_identity,
            headers=tuple(credential_headers),
            anon_header=anon_header,
        )
    elif identity == "client_ip":
        key_func = None
    else:
        raise ValueError(f"unknown rate limit identity: {identity}")
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=global_rpm,
        requests_per_hour=global_rph,
        route_rules=list(route_rules) if route_rules else None,
        exclude_paths=list(exempt_paths),
        key_func=key_func,
        limit_response=limit_response,
    )
