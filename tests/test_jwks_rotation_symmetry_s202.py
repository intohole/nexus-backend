"""S202 IA1 消费侧对称化守底：same-kid 换钥场景 middleware 路径与 uc_sdk 路径行为对称断言。

场景复刻 lion 401 风暴形态：UC 在 kid 不变的前提下换钥（键值变更），消费方缓存中的
旧公钥验签失败——SDK 路径已有签名失败 60s 限频刷钥（auth_verify.py），middleware
路径补齐同纪律；两路径都必须在限频窗内恢复，而非永久 401 至重启。
"""
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

from nexus.middleware_auth import UserTokenVerifier
from nexus.uc_sdk.client import UserCenterSDK

KID = "key-1"


def _pair() -> tuple:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_jwk: dict = jose_jwk.construct(private_key, "RS256").to_dict()
    public_jwk: dict = {k: v for k, v in private_jwk.items() if k in ("kty", "n", "e")}
    public_jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return private_key, public_jwk


def _sign(private_key, payload: dict) -> str:
    return jose_jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KID})


def _payload() -> dict:
    return {"sub": "42", "exp": int(time.time()) + 600}


@pytest.mark.asyncio
async def test_middleware_recovers_after_same_kid_key_change(monkeypatch) -> None:
    old_key, old_jwk = _pair()
    new_key, new_jwk = _pair()
    verifier = UserTokenVerifier(base_url="http://uc.internal", jwt_secret="")
    verifier._jwks = {KID: old_jwk}
    verifier._jwks_fetched_at = time.time() - 999

    async def fake_refresh() -> None:
        verifier._jwks = {KID: new_jwk}
        verifier._jwks_fetched_at = time.time()

    monkeypatch.setattr(verifier, "_refresh_jwks", fake_refresh)
    token = _sign(new_key, _payload())
    assert await verifier.verify(token) is True


@pytest.mark.asyncio
async def test_middleware_signature_fail_refresh_is_rate_limited(monkeypatch) -> None:
    old_key, old_jwk = _pair()
    new_key, new_jwk = _pair()
    third_key, _ = _pair()
    verifier = UserTokenVerifier(base_url="http://uc.internal", jwt_secret="")
    verifier._jwks = {KID: old_jwk}
    verifier._jwks_fetched_at = time.time() - 999
    calls = {"n": 0}

    async def fake_refresh() -> None:
        calls["n"] += 1
        verifier._jwks = {KID: new_jwk}
        verifier._jwks_fetched_at = time.time()

    monkeypatch.setattr(verifier, "_refresh_jwks", fake_refresh)
    token = _sign(new_key, _payload())
    assert await verifier.verify(token) is True
    assert calls["n"] == 1
    stale = _sign(third_key, _payload())
    assert await verifier.verify(stale) is False
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_uc_sdk_reference_path_recovers_after_same_kid_key_change(monkeypatch) -> None:
    old_key, old_jwk = _pair()
    new_key, new_jwk = _pair()
    sdk = UserCenterSDK(base_url="http://uc-test", app_key="app_test", app_secret="s")
    sdk._jwks_by_kid = {KID: old_jwk}
    sdk._jwks_fetched_at = time.time() - 999
    jwks_doc = {"keys": [new_jwk]}
    request = httpx.Request("GET", "http://uc-test/.well-known/jwks.json")

    class _FakeClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, json=jwks_doc, request=request)

    async def _get_client():
        return _FakeClient()

    monkeypatch.setattr(sdk, "_get_client", _get_client)
    token = _sign(new_key, _payload())
    result = await sdk.verify_token(token)
    assert result.get("success") is True, result
