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
async def test_unreachable_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(BrowserHubError) as exc:
        await _client(handler).snapshot("https://a.com")
    assert "不可达" in str(exc.value)
