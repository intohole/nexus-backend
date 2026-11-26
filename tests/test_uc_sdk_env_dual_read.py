"""S207 IA4：uc_sdk env 键双读兼容（UC__JWT_SECRET 优先+UC_JWT_SECRET 兜底）+IA11 限频闭合。

契约（handoff/17 r87 判例对齐）：注入正典=嵌套 UC__*；扁平 UC_* 为兼容兜底；
双形态并存时优先嵌套；启动日志只显影命中键名不显影值。
"""
from __future__ import annotations

import logging

import pytest

import nexus.uc_sdk_helper as helper


@pytest.fixture(autouse=True)
def _reset_helper_state(monkeypatch):
    async def _noop_bootstrap(sdk) -> None:
        return None

    monkeypatch.setattr(helper, "bootstrap_sdk", _noop_bootstrap)
    for key in ("UC__JWT_SECRET", "UC_JWT_SECRET", "UC__APP_SECRET", "UC_APP_SECRET",
                "UC__APP_KEY", "UC__BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    yield
    helper._sdk = None


def _sdk_jwt_secret() -> str:
    return getattr(helper._sdk, "jwt_secret_key", None)


def test_flat_key_alone_is_picked_up(monkeypatch):
    monkeypatch.setenv("UC_JWT_SECRET", "flat-secret")
    helper.init_uc_sdk()
    assert _sdk_jwt_secret() == "flat-secret"


def test_nested_key_alone_is_picked_up(monkeypatch):
    monkeypatch.setenv("UC__JWT_SECRET", "nested-secret")
    helper.init_uc_sdk()
    assert _sdk_jwt_secret() == "nested-secret"


def test_nested_wins_over_flat(monkeypatch):
    monkeypatch.setenv("UC__JWT_SECRET", "nested-secret")
    monkeypatch.setenv("UC_JWT_SECRET", "flat-secret")
    helper.init_uc_sdk()
    assert _sdk_jwt_secret() == "nested-secret"


def test_neither_configured_gives_empty(monkeypatch):
    helper.init_uc_sdk()
    assert _sdk_jwt_secret() == ""


def test_app_secret_dual_read(monkeypatch):
    monkeypatch.setenv("UC_APP_SECRET", "flat-app-secret")
    helper.init_uc_sdk()
    assert getattr(helper._sdk, "app_secret", None) == "flat-app-secret"


def test_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("UC__JWT_SECRET", "nested-secret")
    helper.init_uc_sdk(jwt_secret="explicit-secret")
    assert _sdk_jwt_secret() == "explicit-secret"


def test_hit_key_name_logged_not_value(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="nexus.uc_sdk")
    monkeypatch.setenv("UC_JWT_SECRET", "flat-secret-value-should-not-appear")
    helper.init_uc_sdk()
    hit_lines = [r for r in caplog.records if "UC_JWT_SECRET" in r.getMessage()]
    assert hit_lines, "启动日志未显影命中键名"
    assert "flat-secret-value-should-not-appear" not in caplog.text


def test_refresh_jwks_failure_still_enters_rate_limit_window(monkeypatch):
    from nexus.uc_sdk import UserCenterSDK

    sdk = UserCenterSDK(base_url="http://127.0.0.1:1", app_key="k", app_secret="s")

    async def _fake_get_client():
        return _FakeClient()

    monkeypatch.setattr(sdk, "_get_client", _fake_get_client)
    import time as _time
    before = _time.time()
    import asyncio
    asyncio.run(sdk._refresh_jwks())
    assert sdk._jwks_fetched_at >= before, "失败后时钟未置=限频闸永不闭合（IA11）"


class _FakeClient:
    async def get(self, path):
        raise ConnectionError("uc down")
