"""限流装配原语守底：身份键层次（凭证哈希→匿名 client_ip）+装配面单源。

六仓克隆样板（MiaoBi/gezhi/codeBlock/xunji/nexus-agent/beeMemory 同构
setup_rate_limit）退役面；业界对齐（凭证哈希优先/匿名 per-IP/可信代理链最右 IP）。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from nexus.rate_limit import PathRule, RateLimitMiddleware, assemble_rate_limit
from nexus.rate_limit.keys import credential_identity


def _request(headers: dict[str, str] | None = None, host: str = "203.0.113.9") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "client": (host, 12345),
        "query_string": b"",
    }
    return Request(scope)


def test_credential_hash_identity_prefers_authorization():
    req = _request({"Authorization": "Bearer token-abc"})
    assert credential_identity(req) == "c:" + hashlib.sha256(b"Bearer token-abc").hexdigest()[:16]


def test_credential_hash_identity_extra_headers_chain():
    req = _request({"X-Service-Token": "svc-1"})
    assert credential_identity(req, headers=("Authorization", "X-Service-Token")) == (
        "c:" + hashlib.sha256(b"svc-1").hexdigest()[:16]
    )


def test_credential_identity_anon_header_fallback():
    req = _request({"X-Anon-Id": "anon-123"})
    assert credential_identity(req, anon_header="X-Anon-Id") == "a:" + "anon-123"[:64]


def test_credential_identity_empty_anon_falls_to_ip():
    req = _request({"X-Anon-Id": ""}, host="198.51.100.7")
    assert credential_identity(req, anon_header="X-Anon-Id") == "198.51.100.7"


def test_credential_identity_anonymous_falls_to_client_ip():
    req = _request(host="198.51.100.7")
    assert credential_identity(req) == "198.51.100.7"


def test_credential_identity_xff_rightmost_public():
    req = _request({"X-Forwarded-For": "10.0.0.1, 198.51.100.7"}, host="127.0.0.1")
    assert credential_identity(req) == "198.51.100.7"


def test_assemble_registers_middleware_with_defaults():
    app = FastAPI()
    assemble_rate_limit(app, [PathRule(prefix="/api/v1/posters/", requests_per_minute=15)])
    mws = [m.cls for m in app.user_middleware]
    assert RateLimitMiddleware in mws
    mw = next(m for m in app.user_middleware if m.cls is RateLimitMiddleware)
    assert mw.kwargs["requests_per_minute"] == 120
    assert mw.kwargs["requests_per_hour"] == 0
    assert mw.kwargs["route_rules"] == [PathRule(prefix="/api/v1/posters/", requests_per_minute=15)]
    assert mw.kwargs["exclude_paths"] == ["/health", "/static", "/favicon", "/docs", "/openapi.json"]
    assert callable(mw.kwargs["key_func"])


def test_assemble_custom_exempt_and_headers_passthrough():
    app = FastAPI()
    assemble_rate_limit(
        app,
        exempt_paths=("/health", "/s-media"),
        credential_headers=("Authorization", "X-Service-Token"),
    )
    mw = next(m for m in app.user_middleware if m.cls is RateLimitMiddleware)
    assert mw.kwargs["exclude_paths"] == ["/health", "/s-media"]
    req = _request({"X-Service-Token": "svc-1"})
    assert mw.kwargs["key_func"](req) == "c:" + hashlib.sha256(b"svc-1").hexdigest()[:16]


def test_assemble_client_ip_identity_passes_no_key_func():
    app = FastAPI()
    assemble_rate_limit(app, identity="client_ip")
    mw = next(m for m in app.user_middleware if m.cls is RateLimitMiddleware)
    assert mw.kwargs["key_func"] is None


def test_assemble_unknown_identity_raises():
    app = FastAPI()
    with pytest.raises(ValueError):
        assemble_rate_limit(app, identity="composite")


def test_assemble_limit_response_passthrough():
    app = FastAPI()

    def _resp(request, info):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=429, content={"custom": True})

    assemble_rate_limit(app, limit_response=_resp)
    mw = next(m for m in app.user_middleware if m.cls is RateLimitMiddleware)
    assert mw.kwargs["limit_response"] is _resp


def test_assemble_end_to_end_429_and_identity_isolation():
    app = FastAPI()
    assemble_rate_limit(
        app,
        [PathRule(prefix="/api/chat", requests_per_minute=2)],
        global_rpm=0,
    )

    @app.post("/api/chat/send")
    async def _send():
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/api/chat/send", headers={"Authorization": "Bearer u1"}).status_code == 200
    assert client.post("/api/chat/send", headers={"Authorization": "Bearer u1"}).status_code == 200
    assert client.post("/api/chat/send", headers={"Authorization": "Bearer u1"}).status_code == 429
    assert client.post("/api/chat/send", headers={"Authorization": "Bearer u2"}).status_code == 200
    other = client.post("/api/chat/send", headers={"X-Forwarded-For": "198.51.100.8"})
    assert other.status_code == 200
