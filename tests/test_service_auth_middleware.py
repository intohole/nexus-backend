from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from jose import jwt as jose_jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from nexus.middleware_auth import ServiceAuthMiddleware, UserTokenVerifier

SERVICE_TOKEN = "svc-token-abc123"
KID = "test-kid"


class StubVerifier:
    def __init__(self, accepted: set[str], service_accepted: set[str] | None = None) -> None:
        self._accepted = accepted
        self._service_accepted = service_accepted or set()
        self.configured = True

    async def verify(self, token: str) -> bool:
        return token in self._accepted

    async def verify_service(self, token: str) -> bool:
        return token in self._service_accepted


def build_app(**kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ServiceAuthMiddleware, service_token=SERVICE_TOKEN, **kwargs)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> dict[str, str]:
        return {"page": "index"}

    @app.get("/docs")
    async def docs() -> dict[str, str]:
        return {"page": "docs"}

    @app.get("/redoc")
    async def redoc() -> dict[str, str]:
        return {"page": "redoc"}

    @app.get("/openapi.json")
    async def openapi() -> dict[str, str]:
        return {"schema": "openapi"}

    @app.get("/api/v1/namespaces")
    async def namespaces() -> dict[str, str]:
        return {"data": "namespaces"}

    @app.get("/api/_internal/llm-metrics")
    async def metrics() -> dict[str, str]:
        return {"metrics": "llm"}

    return app


async def call(app: FastAPI, path: str, headers: dict[str, str] | None = None,
               cookies: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {}, cookies=cookies or {})


@pytest.mark.asyncio
async def test_anonymous_request_denied() -> None:
    resp = await call(build_app(), "/api/v1/namespaces")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid service token"


@pytest.mark.asyncio
async def test_forged_bearer_token_denied() -> None:
    app = build_app()
    for forged in ["test", "x", "1", "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0."]:
        resp = await call(app, "/api/v1/namespaces", {"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401, forged


@pytest.mark.asyncio
async def test_forged_bearer_token_denied_even_with_user_tokens_enabled() -> None:
    app = build_app(allow_user_tokens=True, token_verifier=StubVerifier({"real-user-token"}))
    resp = await call(app, "/api/v1/namespaces", {"Authorization": "Bearer test"})
    assert resp.status_code == 401
    resp = await call(app, "/api/v1/namespaces", {"Authorization": "Bearer real-user-token"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_user_token_denied_when_user_tokens_disabled() -> None:
    app = build_app(allow_user_tokens=False, token_verifier=StubVerifier({"real-user-token"}))
    resp = await call(app, "/api/v1/namespaces", {"Authorization": "Bearer real-user-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_service_jwt_accepted_via_verifier() -> None:
    app = build_app(
        token_verifier=StubVerifier({"real-user-token"}, service_accepted={"svc-jwt-token"}),
    )
    assert (await call(app, "/api/v1/namespaces", {"X-Service-Token": "svc-jwt-token"})).status_code == 200
    assert (await call(app, "/api/v1/namespaces", {"Authorization": "Bearer svc-jwt-token"})).status_code == 200


@pytest.mark.asyncio
async def test_user_token_not_accepted_as_service_when_service_verifier_configured() -> None:
    app = build_app(
        allow_user_tokens=True,
        token_verifier=StubVerifier({"real-user-token"}, service_accepted={"svc-jwt-token"}),
    )
    assert (await call(app, "/api/v1/namespaces", {"X-Service-Token": "real-user-token"})).status_code == 401
    assert (await call(app, "/api/v1/namespaces", {"Authorization": "Bearer real-user-token"})).status_code == 200


@pytest.mark.asyncio
async def test_service_token_accepted_in_all_forms() -> None:
    app = build_app()
    assert (await call(app, "/api/v1/namespaces", {"X-Service-Token": SERVICE_TOKEN})).status_code == 200
    assert (await call(app, "/api/v1/namespaces", {"Authorization": f"Bearer {SERVICE_TOKEN}"})).status_code == 200
    assert (await call(app, "/api/v1/namespaces", cookies={"service_token": SERVICE_TOKEN})).status_code == 200


@pytest.mark.asyncio
async def test_docs_paths_are_not_anonymous() -> None:
    app = build_app()
    for path in ["/docs", "/redoc", "/openapi.json", "/api/_internal/llm-metrics"]:
        resp = await call(app, path)
        assert resp.status_code == 401, path
    assert (await call(app, "/docs", {"X-Service-Token": SERVICE_TOKEN})).status_code == 200


@pytest.mark.asyncio
async def test_whitelisted_paths_stay_public() -> None:
    app = build_app()
    for path in ["/health", "/", "/index.html", "/static/app.js"]:
        resp = await call(app, path)
        assert resp.status_code in (200, 404), path
        assert resp.status_code != 401, path


def _rsa_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from jose import jwk as jose_jwk

    private_jwk: dict[str, str] = jose_jwk.construct(private_key, "RS256").to_dict()
    public_jwk: dict[str, str] = {
        k: v for k, v in private_jwk.items() if k in ("kty", "n", "e")
    }
    public_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return private_key, public_jwk


def _sign(private_key, payload: dict[str, object]) -> str:
    return jose_jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KID})


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unsigned_token(payload: dict[str, object]) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    return f"{header}.{body}."


@pytest.mark.asyncio
async def test_verifier_accepts_valid_rs256_token(monkeypatch) -> None:
    private_key, public_jwk = _rsa_pair()
    verifier = UserTokenVerifier(base_url="http://uc.internal", jwt_secret="")

    async def fake_refresh() -> None:
        verifier._jwks = {KID: public_jwk}
        verifier._jwks_fetched_at = time.time()

    monkeypatch.setattr(verifier, "_refresh_jwks", fake_refresh)
    token = _sign(private_key, {"sub": "42", "exp": int(time.time()) + 600})
    assert await verifier.verify(token) is True


@pytest.mark.asyncio
async def test_verifier_rejects_foreign_signature(monkeypatch) -> None:
    _, public_jwk = _rsa_pair()
    other_key, _ = _rsa_pair()
    verifier = UserTokenVerifier(base_url="http://uc.internal")

    async def fake_refresh() -> None:
        verifier._jwks = {KID: public_jwk}
        verifier._jwks_fetched_at = time.time()

    monkeypatch.setattr(verifier, "_refresh_jwks", fake_refresh)
    token = _sign(other_key, {"sub": "42", "exp": int(time.time()) + 600})
    assert await verifier.verify(token) is False


@pytest.mark.asyncio
async def test_verifier_rejects_expired_and_unsigned_tokens(monkeypatch) -> None:
    private_key, public_jwk = _rsa_pair()
    verifier = UserTokenVerifier(base_url="http://uc.internal", jwt_secret="hs-secret")

    async def fake_refresh() -> None:
        verifier._jwks = {KID: public_jwk}
        verifier._jwks_fetched_at = time.time()

    monkeypatch.setattr(verifier, "_refresh_jwks", fake_refresh)
    expired = _sign(private_key, {"sub": "42", "exp": int(time.time()) - 10})
    assert await verifier.verify(expired) is False
    unsigned = _unsigned_token({"sub": "42", "exp": int(time.time()) + 600})
    assert await verifier.verify(unsigned) is False
    assert await verifier.verify("not-a-jwt") is False
    assert await verifier.verify("") is False


@pytest.mark.asyncio
async def test_verifier_hs256_fallback_and_fail_closed() -> None:
    verifier = UserTokenVerifier(base_url="", jwt_secret="hs-secret")
    token = jose_jwt.encode(
        {"sub": "42", "exp": int(time.time()) + 600}, "hs-secret", algorithm="HS256",
    )
    assert await verifier.verify(token) is True
    wrong = jose_jwt.encode(
        {"sub": "42", "exp": int(time.time()) + 600}, "other-secret", algorithm="HS256",
    )
    assert await verifier.verify(wrong) is False
    unconfigured = UserTokenVerifier()
    assert unconfigured.configured is False
    assert await unconfigured.verify(token) is False