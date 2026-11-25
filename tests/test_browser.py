"""nexus.browser 客户端契约守底（r248 R17，补 r247 零测试欠账）。

五方法 payload 形状、非 200 错误面、asset_app/include_aria/aria_mode 透传、
mock transport 全离线（评测离线隔离纪律）。
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

os.environ.setdefault("SERVICE_TOKEN", "test-browser-client-token")

from nexus.browser import BrowserHubClient, BrowserHubError

BASE = "http://hub.test"


def _client(handler) -> BrowserHubClient:
    return BrowserHubClient(base_url=BASE, transport=httpx.MockTransport(handler))


def _capture(handler):
    captured: dict = {}

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content) if request.content else {}
        captured["token"] = request.headers.get("X-Service-Token", "")
        return handler(request)

    return _client(wrapped), captured


@pytest.mark.asyncio
async def test_snapshot_payload_and_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"asset_rel": "xunji/u/shot.jpg", "title": "t"})

    client, captured = _capture(handler)
    out = await client.snapshot(
        "https://songguokr.com",
        viewport={"width": 1440, "height": 900},
        asset_app="xunji",
        include_aria=True,
        storage_state={"cookies": [], "origins": []},
    )
    assert captured["path"] == "/api/browser/v1/snapshot"
    assert captured["payload"]["url"] == "https://songguokr.com"
    assert captured["payload"]["asset_app"] == "xunji"
    assert captured["payload"]["include_aria"] is True
    assert captured["payload"]["viewport"] == {"width": 1440, "height": 900}
    assert captured["payload"]["storage_state"] == {"cookies": [], "origins": []}
    assert captured["token"] == "test-browser-client-token"
    assert out["asset_rel"] == "xunji/u/shot.jpg"


@pytest.mark.asyncio
async def test_aria_payload_defaults_to_ai_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"aria_yaml": "- heading \"t\" [ref=e1]"})

    client, captured = _capture(handler)
    out = await client.aria("https://songguokr.com", aria_depth=5)
    assert captured["path"] == "/api/browser/v1/aria"
    assert captured["payload"]["aria_mode"] == "ai"
    assert captured["payload"]["aria_depth"] == 5
    assert "[ref=e1]" in out["aria_yaml"]


@pytest.mark.asyncio
async def test_matrix_extract_pdf_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/matrix"):
            return httpx.Response(200, json={"cells": []})
        if request.url.path.endswith("/extract"):
            return httpx.Response(200, json={"text": "body"})
        return httpx.Response(200, json={"asset_rel": "browserhub/u/x.pdf"})

    client = _client(handler)
    assert await client.matrix("https://a.com", themes=["light"]) == {"cells": []}
    assert await client.extract("https://a.com") == {"text": "body"}
    assert (await client.pdf("https://a.com"))["asset_rel"] == "browserhub/u/x.pdf"


@pytest.mark.asyncio
async def test_error_face_carries_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "theme 仅支持 {'light', 'dark', 'auto'}"})

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).snapshot("https://a.com", theme="sepia")
    assert "theme" in str(exc.value)


@pytest.mark.asyncio
async def test_error_face_non_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).snapshot("https://a.com")
    assert "bad gateway" in str(exc.value)


@pytest.mark.asyncio
async def test_busy_429_carries_structured_backoff():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "detail": {
                    "code": "busy",
                    "message": "渲染排队超过 20s",
                    "retry_after_ms": 20000,
                    "queue_depth": 8,
                }
            },
        )

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).snapshot("https://a.com")
    assert exc.value.status_code == 429
    assert exc.value.retry_after_ms == 20000
    assert exc.value.queue_depth == 8


@pytest.mark.asyncio
async def test_extract_html_mode_and_selector_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"html": "<h1>x</h1>"})

    client, captured = _capture(handler)
    out = await client.extract(
        "https://a.com",
        fmt="html",
        storage_state={"cookies": [], "origins": []},
        wait_selector="#main",
        allow_private=False,
    )
    assert captured["payload"]["format"] == "html"
    assert captured["payload"]["storage_state"] == {"cookies": [], "origins": []}
    assert captured["payload"]["wait_selector"] == "#main"
    assert captured["payload"]["allow_private"] is False
    assert "<h1>" in out["html"]


@pytest.mark.asyncio
async def test_block_media_and_allow_private_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"asset_rel": "r"})

    client, captured = _capture(handler)
    await client.snapshot("https://a.com", block_media=True, allow_private=False)
    assert captured["payload"]["block_media"] is True
    assert captured["payload"]["allow_private"] is False
    await client.aria("https://a.com", block_media=False)
    assert captured["payload"]["block_media"] is False
    await client.pdf("https://a.com", allow_private=True)
    assert captured["payload"]["allow_private"] is True


@pytest.mark.asyncio
async def test_session_open_act_close_paths():
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content) if request.content else {})
        if request.method == "POST" and request.url.path.endswith("/sessions"):
            return httpx.Response(200, json={"session_id": "abc123", "aria_yaml": "- heading [ref=e1]"})
        if request.url.path.endswith("/act"):
            return httpx.Response(200, json={"session_id": "abc123", "action": "click", "aria_yaml": "- heading [ref=e1]"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"closed": True})
        return httpx.Response(404, json={"detail": "nope"})

    client = _client(handler)
    opened = await client.open_session("https://a.com", storage_state={"cookies": [], "origins": []})
    assert opened["session_id"] == "abc123"
    acted = await client.act("abc123", "click", ref="e1")
    assert acted["action"] == "click"
    assert await client.close_session("abc123") == {"closed": True}
    assert payloads[0]["storage_state"] == {"cookies": [], "origins": []}


@pytest.mark.asyncio
async def test_session_act_payload_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client, captured = _capture(handler)
    await client.act("abc123", "navigate", url="https://b.com")
    assert captured["payload"] == {"action": "navigate", "url": "https://b.com"}
    await client.act("abc123", "wait", wait_ms=500)
    assert captured["payload"] == {"action": "wait", "wait_ms": 500}
    await client.act("abc123", "scroll", direction="up", amount=300)
    assert captured["payload"]["direction"] == "up"


@pytest.mark.asyncio
async def test_session_gone_410_carries_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            410,
            json={"detail": {"code": "session_gone", "message": "session 已失效", "hint": "请重建会话"}},
        )

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).act("dead", "click", ref="e1")
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_unreachable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).snapshot("https://a.com")
    assert "不可达" in str(exc.value)
