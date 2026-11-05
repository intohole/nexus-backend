"""org 声明低频复核守底（r20 遗留①收口）：漂移校正/节流/退避/fail-open/双层接线。

口径对齐教训 #842：失效类改动的验收是「被移出成员的 org 声明在复核窗内
归零」，不是「标志位已翻转」。远程层 mock `_verify_token_remote`（UC 侧
/token/validate 已于 r20 改 DB 实时派生，生产为权威源）。
"""
import asyncio
import time

from jose import jwt as jose_jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from nexus.uc_sdk.client import UserCenterSDK
from nexus.uc_sdk.resilience import OrgRecheckCache

_HS_SECRET = "hs-recheck-secret"


def _make_sdk() -> UserCenterSDK:
    return UserCenterSDK(
        base_url="http://uc.test", app_key="k", app_secret="s",
        jwt_secret_key=_HS_SECRET,
    )


def _org_token(org_id=66, org_role="member", jti="jti-recheck-1") -> str:
    return jose_jwt.encode(
        {"sub": "12", "app_id": 3, "role": "user", "type": "access",
         "jti": jti, "org_id": org_id, "org_role": org_role},
        _HS_SECRET, algorithm="HS256")


def _remote_reply(org_id, org_role=None):
    async def fake(token, permission=None, timeout=None):
        return {"success": True, "org_id": org_id, "org_role": org_role}
    return fake


def test_drifted_org_corrected_within_recheck():
    sdk = _make_sdk()
    sdk._verify_token_remote = _remote_reply(None, None)
    result = asyncio.run(sdk.verify_token(_org_token()))
    assert result["success"] is True
    assert result["org_id"] is None
    assert result["org_role"] is None


def test_org_transfer_corrected_to_new_org():
    sdk = _make_sdk()
    sdk._verify_token_remote = _remote_reply(77, "admin")
    result = asyncio.run(sdk.verify_token(_org_token(org_id=66, org_role="member")))
    assert result["success"] is True
    assert result["org_id"] == 77
    assert result["org_role"] == "admin"


def test_recheck_uses_bounded_timeout():
    sdk = _make_sdk()
    seen = {}

    async def fake_request(method, path, **kwargs):
        seen.update(kwargs)
        return {"success": True, "data": {"org_id": None, "org_role": None}}

    sdk._request = fake_request
    result = asyncio.run(sdk.verify_token(_org_token()))
    assert seen.get("timeout") == 3.0
    assert result["success"] is True
    assert result["org_id"] is None


def test_recheck_throttled_within_ttl():
    sdk = _make_sdk()
    calls = []

    async def fake(token, permission=None, timeout=None):
        calls.append(token)
        return {"success": True, "org_id": 66, "org_role": "member"}

    sdk._verify_token_remote = fake
    token = _org_token()
    first = asyncio.run(sdk.verify_token(token))
    second = asyncio.run(sdk.verify_token(token))
    assert first["org_id"] == 66 and second["org_id"] == 66
    assert len(calls) == 1


def test_recheck_reruns_after_ttl_expiry():
    sdk = _make_sdk()
    sdk._org_recheck_cache = OrgRecheckCache(ttl=0.01)
    calls = []

    async def fake(token, permission=None, timeout=None):
        calls.append(token)
        return {"success": True, "org_id": 66, "org_role": "member"}

    sdk._verify_token_remote = fake
    token = _org_token()
    asyncio.run(sdk.verify_token(token))
    time.sleep(0.03)
    asyncio.run(sdk.verify_token(token))
    assert len(calls) == 2


def test_recheck_fail_open_keeps_local_claims():
    sdk = _make_sdk()

    async def boom(token, permission=None, timeout=None):
        raise RuntimeError("uc down")

    sdk._verify_token_remote = boom
    result = asyncio.run(sdk.verify_token(_org_token()))
    assert result["success"] is True
    assert result["org_id"] == 66
    assert result["org_role"] == "member"


def test_recheck_failure_backs_off_same_window():
    sdk = _make_sdk()
    calls = []

    async def boom(token, permission=None, timeout=None):
        calls.append(token)
        raise RuntimeError("uc down")

    sdk._verify_token_remote = boom
    token = _org_token()
    asyncio.run(sdk.verify_token(token))
    asyncio.run(sdk.verify_token(token))
    assert len(calls) == 1


def test_personal_token_skips_recheck():
    sdk = _make_sdk()

    async def boom(token, permission=None, timeout=None):
        raise AssertionError("个人态 token 不应触发 org 复核出站")

    sdk._verify_token_remote = boom
    token = jose_jwt.encode(
        {"sub": "12", "app_id": 3, "role": "user", "type": "access", "jti": "jti-p-1"},
        _HS_SECRET, algorithm="HS256")
    result = asyncio.run(sdk.verify_token(token))
    assert result["success"] is True
    assert result["org_id"] is None


def test_token_without_jti_skips_recheck():
    sdk = _make_sdk()

    async def boom(token, permission=None, timeout=None):
        raise AssertionError("无 jti 的 token 不应触发 org 复核出站")

    sdk._verify_token_remote = boom
    token = jose_jwt.encode(
        {"sub": "12", "app_id": 3, "role": "user", "type": "access", "org_id": 66, "org_role": "member"},
        _HS_SECRET, algorithm="HS256")
    result = asyncio.run(sdk.verify_token(token))
    assert result["success"] is True
    assert result["org_id"] == 66


def test_jwks_layer_also_applies_recheck():
    sdk = _make_sdk()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pub_nums = key.public_key().public_numbers()
    import base64

    def _bint(n: int) -> str:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "alg": "RS256", "kid": "recheck-kid", "n": _bint(pub_nums.n), "e": _bint(pub_nums.e)}
    sdk._jwks_by_kid = {"recheck-kid": jwk}
    token = jose_jwt.encode(
        {"sub": "12", "app_id": 3, "role": "user", "type": "access",
         "jti": "jti-rs-1", "org_id": 66, "org_role": "member"},
        key, algorithm="RS256", headers={"kid": "recheck-kid"})
    sdk._verify_token_remote = _remote_reply(None, None)
    result = asyncio.run(sdk.verify_token(token))
    assert result["success"] is True
    assert result["org_id"] is None


def test_remote_invalid_response_fails_open():
    sdk = _make_sdk()

    async def invalid(token, permission=None, timeout=None):
        return {"success": False, "detail": "Token expired"}

    sdk._verify_token_remote = invalid
    result = asyncio.run(sdk.verify_token(_org_token()))
    assert result["success"] is True
    assert result["org_id"] == 66
