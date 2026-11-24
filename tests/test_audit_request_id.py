"""审计上报 request_id+失败计数守底（S200·r243 差距 11）。

差距 11：审计事件 payload 曾无 request_id（不可回放），上报失败静默
return False 无计数（r33「失败必须自显」判例同族）。修复=payload 拼接
trace 单源 request_id+模块级失败计数器带读数函数。
"""
from __future__ import annotations

import httpx
import pytest

import nexus.audit as audit_mod


class _CaptureHttp:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.payloads: list[dict[str, object]] = []

    async def post(self, url: str, headers: object = None, json: object = None) -> httpx.Response:
        if self.exc is not None:
            raise self.exc
        self.payloads.append(dict(json))  # type: ignore[arg-type]
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.fixture()
def _reset_fail_count() -> None:
    audit_mod.reset_fail_count()
    yield None
    audit_mod.reset_fail_count()


@pytest.mark.asyncio
async def test_audit_payload_carries_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _CaptureHttp()
    monkeypatch.setattr(audit_mod, "get_service_token", _fake_token)
    monkeypatch.setattr(audit_mod.httpx, "AsyncClient", lambda timeout=None: _Client(http))

    ok = await audit_mod.log_audit(action="test.action", user_id=1)

    assert ok is True
    rid = http.payloads[0].get("request_id")
    assert isinstance(rid, str) and rid


@pytest.mark.asyncio
async def test_audit_failure_increments_counter(monkeypatch: pytest.MonkeyPatch, _reset_fail_count: None) -> None:
    http = _CaptureHttp(exc=RuntimeError("uc down"))
    monkeypatch.setattr(audit_mod, "get_service_token", _fake_token)
    monkeypatch.setattr(audit_mod.httpx, "AsyncClient", lambda timeout=None: _Client(http))
    before = audit_mod.audit_fail_count()

    ok = await audit_mod.log_audit(action="test.fail")

    assert ok is False
    assert audit_mod.audit_fail_count() == before + 1


@pytest.mark.asyncio
async def test_audit_success_does_not_increment(monkeypatch: pytest.MonkeyPatch, _reset_fail_count: None) -> None:
    http = _CaptureHttp()
    monkeypatch.setattr(audit_mod, "get_service_token", _fake_token)
    monkeypatch.setattr(audit_mod.httpx, "AsyncClient", lambda timeout=None: _Client(http))

    await audit_mod.log_audit(action="test.ok")

    assert audit_mod.audit_fail_count() == 0


async def _fake_token() -> str:
    return "test-service-token"


class _Client:
    def __init__(self, handler: _CaptureHttp) -> None:
        self._handler = handler

    async def __aenter__(self) -> "_CaptureHttp":
        return self._handler

    async def __aexit__(self, *args: object) -> None:
        return None
