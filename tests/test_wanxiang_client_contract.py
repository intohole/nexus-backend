"""nexus.wanxiang client 契约守底:alias 透传与 resolve 原语(r6 判例)。

真链 E2E 在 wanxiang 仓(本地起服全链);此处钉 HTTP 契约形态——
请求字段/路径/404 语义,防 client 与中台契约漂移。transport 注入是
WanxiangClient 的官方测试位(httpx.MockTransport),非 mock 业务数据。"""
from __future__ import annotations

import httpx
import pytest

import nexus.wanxiang as wx


@pytest.fixture()
def token(monkeypatch: pytest.MonkeyPatch):
    async def _fake() -> str:
        return "tok"

    monkeypatch.setattr(wx, "get_service_token", _fake)


def _client(handler) -> wx.WanxiangClient:
    return wx.WanxiangClient(base_url="http://wx.test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_upload_passes_alias_in_form(token):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = request.read().decode()
        return httpx.Response(200, json={"success": True, "data": {"id": "a1", "alias": "runner/win"}})

    out = await _client(handler).upload("goldenFish", "public", "pkg.zip", b"x", "application/zip", alias="runner/win")
    assert out["alias"] == "runner/win"
    assert seen["url"].endswith("/api/v1/assets/upload")
    assert 'name="alias"' in seen["form"] and "runner/win" in seen["form"]


@pytest.mark.asyncio
async def test_upload_without_alias_keeps_form_clean(token):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["form"] = request.read().decode()
        return httpx.Response(200, json={"success": True, "data": {"id": "a1"}})

    await _client(handler).upload("app", "u1", "f.png", b"x", "image/png")
    assert 'name="alias"' not in seen["form"]


@pytest.mark.asyncio
async def test_resolve_returns_data_and_sends_alias(token):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["app"] = request.headers.get("X-App-Name")
        return httpx.Response(200, json={"success": True, "data": {"id": "a2", "url": "https://x", "filename": "p.zip"}})

    out = await _client(handler).resolve_alias("goldenFish", "runner/win")
    assert out["id"] == "a2"
    assert "/api/v1/assets/resolve" in seen["url"] and "runner" in seen["url"]
    assert seen["app"] == "goldenFish"


@pytest.mark.asyncio
async def test_resolve_404_raises_honest_error(token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "别名不存在: runner/win"})

    with pytest.raises(wx.WanxiangError, match="别名不存在"):
        await _client(handler).resolve_alias("goldenFish", "runner/win")


@pytest.mark.asyncio
async def test_resolve_5xx_raises_with_detail(token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "bad gateway"})

    with pytest.raises(wx.WanxiangError, match="别名解析失败"):
        await _client(handler).resolve_alias("goldenFish", "runner/win")
