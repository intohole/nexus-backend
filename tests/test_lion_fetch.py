"""fetch_llm_config 错误信封归一契约测试。"""

import asyncio

from nexus.lion_sdk import fetch_llm_config


class _FakeLion:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_ready_config(self, key, prefer_gateway=True):
        return self._result


def _patch_sdk(monkeypatch, result=None, exc=None):
    from nexus.lion_sdk import client as client_mod

    class _Factory:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            if exc:
                raise exc
            return _FakeLion(result)

        async def __aexit__(self, *a):
            return False

        async def get_ready_config(self, key, prefer_gateway=True):
            return result

    monkeypatch.setattr(client_mod, "LionSDK", _Factory)


def test_error_envelope_returns_none(monkeypatch):
    _patch_sdk(monkeypatch, result={"success": False, "detail": "Cannot connect"})
    assert asyncio.run(fetch_llm_config("chat")) is None


def test_success_returns_config(monkeypatch):
    cfg = {"model": "glm-4", "api_key": "k", "base_url": "https://x/v1"}
    _patch_sdk(monkeypatch, result=cfg)
    assert asyncio.run(fetch_llm_config("chat")) == cfg


def test_connection_exception_returns_none(monkeypatch):
    _patch_sdk(monkeypatch, exc=ConnectionError("refused"))
    assert asyncio.run(fetch_llm_config("chat")) is None


def test_non_dict_returns_none(monkeypatch):
    _patch_sdk(monkeypatch, result="garbage")
    assert asyncio.run(fetch_llm_config("chat")) is None


def test_env_fallback(monkeypatch):
    captured = {}

    class _Factory:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return _Factory.__new__(_Factory)

        async def __aexit__(self, *a):
            return False

        async def get_ready_config(self, key, prefer_gateway=True):
            return {"success": False, "detail": "x"}

    from nexus.lion_sdk import client as client_mod
    monkeypatch.setattr(client_mod, "LionSDK", _Factory)
    monkeypatch.setenv("LION__BASE_URL", "http://lion-test:9527")
    monkeypatch.setenv("LION__NAMESPACE", "testns")
    asyncio.run(fetch_llm_config("chat"))
    assert captured["base_url"] == "http://lion-test:9527"
    assert captured["namespace"] == "testns"


def test_headers_env_first_static_token(monkeypatch):
    """env-first：LION_SERVICE_TOKEN/SERVICE_TOKEN 命中时不触达 UC JWT 铸造（r28 形态）。"""
    from nexus.lion_sdk import client as client_mod

    calls: list[str] = []

    async def _fail_get_service_token():
        calls.append("minted")
        return "should-not-be-used"

    monkeypatch.setattr(client_mod, "get_service_token", _fail_get_service_token)
    monkeypatch.delenv("LION_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("SERVICE_TOKEN", "cluster-static-token")

    async def _main():
        async with client_mod.LionSDK(base_url="http://lion-test:9527") as lion:
            headers = await lion._headers()
        return headers

    headers = asyncio.run(_main())
    assert headers == {"Authorization": "Bearer cluster-static-token"}
    assert calls == []


def test_headers_lion_token_precedence(monkeypatch):
    """LION_SERVICE_TOKEN 优先于 SERVICE_TOKEN（同值单源约定下的显式覆盖口）。"""
    from nexus.lion_sdk import client as client_mod

    async def _fail_get_service_token():
        return "should-not-be-used"

    monkeypatch.setattr(client_mod, "get_service_token", _fail_get_service_token)
    monkeypatch.setenv("LION_SERVICE_TOKEN", "lion-explicit")
    monkeypatch.setenv("SERVICE_TOKEN", "cluster-static-token")

    async def _main():
        async with client_mod.LionSDK(base_url="http://lion-test:9527") as lion:
            return await lion._headers()

    assert asyncio.run(_main()) == {"Authorization": "Bearer lion-explicit"}


def test_headers_falls_to_uc_jwt_when_env_absent(monkeypatch):
    """env 双缺时走链尾 get_service_token——G3（UC__BASE_URL）落地后的 JWT 迁移通道，禁止删。"""
    from nexus.lion_sdk import client as client_mod

    async def _fake_get_service_token():
        return "uc-minted-jwt"

    monkeypatch.setattr(client_mod, "get_service_token", _fake_get_service_token)
    monkeypatch.delenv("LION_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    async def _main():
        async with client_mod.LionSDK(base_url="http://lion-test:9527") as lion:
            return await lion._headers()

    assert asyncio.run(_main()) == {"Authorization": "Bearer uc-minted-jwt"}


def test_headers_empty_when_all_sources_absent(monkeypatch):
    from nexus.lion_sdk import client as client_mod

    async def _empty_get_service_token():
        return ""

    monkeypatch.setattr(client_mod, "get_service_token", _empty_get_service_token)
    monkeypatch.delenv("LION_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    async def _main():
        async with client_mod.LionSDK(base_url="http://lion-test:9527") as lion:
            return await lion._headers()

    assert asyncio.run(_main()) == {}
