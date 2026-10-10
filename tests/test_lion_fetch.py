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
