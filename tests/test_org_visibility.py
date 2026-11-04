"""org 原语守底：可见性谓词切界 + 包级导出 + org_role 全链透传。"""
import asyncio

from jose import jwt as jose_jwt
from sqlalchemy import Column, Integer, MetaData, String, Table

from nexus import org_visibility, require_permission, get_current_org_id_optional
from nexus.auth import normalize_user_dict
from nexus.uc_sdk.client import UserCenterSDK


_meta = MetaData()
_t = Table("t", _meta, Column("user_id", String(64)), Column("org_id", Integer))


def _sql(clause) -> str:
    return str(clause.compile(dialect=None, compile_kwargs={"literal_binds": True}))


def test_org_visibility_personal_only_when_no_org():
    sql = _sql(org_visibility(_t.c.user_id, "alice"))
    assert "OR" not in sql
    assert "alice" in sql


def test_org_visibility_extends_to_same_org():
    sql = _sql(org_visibility(_t.c.user_id, "alice", _t.c.org_id, 77))
    assert " OR " in sql
    assert "77" in sql


def test_org_visibility_ignores_org_without_column():
    sql = _sql(org_visibility(_t.c.user_id, "alice", None, 77))
    assert "OR" not in sql


def test_package_exports_org_symbols():
    assert callable(require_permission)
    assert callable(get_current_org_id_optional)
    assert callable(org_visibility)


def test_normalize_user_dict_carries_org_role():
    out = normalize_user_dict({"user_id": "1", "org_id": 9, "org_role": "admin"})
    assert out["org_id"] == 9
    assert out["org_role"] == "admin"


def test_sdk_local_verify_carries_org_context():
    sdk = UserCenterSDK(base_url="http://uc.test", app_key="k", app_secret="s", jwt_secret_key="hs-secret")
    token = jose_jwt.encode(
        {"sub": "12", "app_id": 3, "role": "user", "org_id": 66, "org_role": "member", "type": "access"},
        "hs-secret", algorithm="HS256")
    result = asyncio.run(sdk.verify_token(token))
    assert result["success"] is True
    assert result["org_id"] == 66
    assert result["org_role"] == "member"
