"""nexus.wanxiang AssetGateway + uploads 端点族契约守底(r208 收归判例)。

gezhi/xunji 手搓 AssetService/uploads.py 逐字节克隆收归单点后的行为钉:
rel 校验(段数/前缀/穿越)、签名缓存命中、错误转译语义、端点族响应契约。
transport 注入=httpx.MockTransport,非 mock 业务数据。"""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nexus.wanxiang as wx


@pytest.fixture()
def token(monkeypatch: pytest.MonkeyPatch):
    async def _fake() -> str:
        return "tok"

    monkeypatch.setattr(wx, "get_service_token", _fake)


def _gateway(handler, app_name: str = "gezhi") -> wx.AssetGateway:
    return wx.AssetGateway(app_name, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_upload_returns_rel_and_translates_error(token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"rel": "gezhi/short/u1/a.jpg"}})

    gateway = _gateway(handler)
    rel = await gateway.upload("u1", "a.jpg", b"img", "image/jpeg")
    assert rel == "gezhi/short/u1/a.jpg"

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "mid down"})

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _gateway(bad_handler).upload("u1", "a.jpg", b"img", "image/jpeg")
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_upload_missing_rel_raises_502(token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"id": "x"}})

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        await _gateway(handler).upload("u1", "a.jpg", b"img", "image/jpeg")
    assert ei.value.status_code == 502 and "缺少 rel" in ei.value.detail


@pytest.mark.asyncio
async def test_open_stream_rejects_bad_rel(token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    from fastapi import HTTPException

    gateway = _gateway(handler)
    for bad_rel in ("", "gezhi/u1", "gezhi/u1/a/b/c.jpg", "other/short/u1/a.jpg", "gezhi/u1/../y/a.jpg"):
        with pytest.raises(HTTPException) as ei:
            await gateway.open_stream(bad_rel)
        assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_open_stream_streams_upstream_and_caches_sign(token):
    sign_calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/assets/sign" in str(request.url):
            sign_calls.append(1)
            return httpx.Response(200, json={"success": True, "data": {"gezhi/short/u1/a.jpg": {"url": "http://wx.test/serve/x?sig=abc"}}})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"IMGDATA")

    from fastapi import HTTPException

    gateway = _gateway(handler)
    client, upstream = await gateway.open_stream("gezhi/short/u1/a.jpg")
    body = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    assert body == b"IMGDATA"
    assert len(sign_calls) == 1
    cached = gateway._sign_cache["gezhi/short/u1/a.jpg"]
    assert cached[1] > time.time()


@pytest.mark.asyncio
async def test_open_stream_maps_upstream_status(token):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/assets/sign" in str(request.url):
            return httpx.Response(200, json={"success": True, "data": {"gezhi/short/u1/a.jpg": {"url": "http://wx.test/x?sig=a"}}})
        return httpx.Response(404, json={"detail": "no"})

    from fastapi import HTTPException

    gateway = _gateway(handler)
    with pytest.raises(HTTPException) as ei:
        await gateway.open_stream("gezhi/short/u1/a.jpg")
    assert ei.value.status_code == 404 and "文件不存在" in ei.value.detail


@pytest.mark.asyncio
async def test_read_bytes_returns_full_body(token):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/assets/sign" in str(request.url):
            return httpx.Response(200, json={"success": True, "data": {"gezhi/short/u1/a.jpg": {"url": "http://wx.test/x?sig=a"}}})
        return httpx.Response(200, content=b"BYTES-OK")

    out = await _gateway(handler).read_bytes("gezhi/short/u1/a.jpg")
    assert out == b"BYTES-OK"


def _app(token, handler, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    async def uid_dep() -> str:
        return "u1"

    class _StubAuth:
        @staticmethod
        async def get_user_id_required(creds) -> str:
            return "u1"

    monkeypatch.setattr(wx, "get_auth_deps", lambda: _StubAuth())
    app = FastAPI()
    router = wx.make_uploads_router(app_name="gezhi", get_current_uid=uid_dep, max_image_mb=1.0)
    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(wx, "get_wanxiang_base_url", lambda: "http://wx.test")
    import nexus.trace as trace_mod

    monkeypatch.setattr(trace_mod, "outbound_headers", lambda extra=None: {})
    gw = wx._gateways["gezhi"]
    gw._client_override = wx.WanxiangClient(base_url="http://wx.test", transport=httpx.MockTransport(handler))
    gw._transport = httpx.MockTransport(handler)
    return TestClient(app, raise_server_exceptions=False)


def test_endpoint_upload_then_serve_roundtrip(token, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v1/assets/upload" in url:
            assert request.headers["X-App-Name"] == "gezhi"
            return httpx.Response(200, json={"success": True, "data": {"rel": "gezhi/short/u1/p.jpg"}})
        if "/api/v1/assets/sign" in url:
            return httpx.Response(200, json={"success": True, "data": {"gezhi/short/u1/p.jpg": {"url": "http://wx.test/s?sig=z"}}})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"JPEG-BYTES")

    client = _app(token, handler, monkeypatch)
    resp = client.post(
        "/api/v1/uploads",
        files={"file": ("p.jpg", b"RAW", "image/jpeg")},
        headers={"Authorization": "Bearer sess"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "/api/v1/uploads/serve?rel=gezhi/short/u1/p.jpg"
    assert body["rel"] == "gezhi/short/u1/p.jpg"

    served = client.get("/api/v1/uploads/serve", params={"rel": "gezhi/short/u1/p.jpg", "t": "sess"})
    assert served.status_code == 200
    assert served.content == b"JPEG-BYTES"
    assert served.headers["content-type"] == "image/jpeg"
    assert "max-age=60" in served.headers["cache-control"]


def test_endpoint_upload_rejects_bad_mime_and_oversize(token, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {"rel": "gezhi/short/u1/p.jpg"}})

    client = _app(token, handler, monkeypatch)
    bad_mime = client.post(
        "/api/v1/uploads",
        files={"file": ("p.gif", b"R", "image/gif")},
        headers={"Authorization": "Bearer sess"},
    )
    assert bad_mime.status_code == 400
    oversize = client.post(
        "/api/v1/uploads",
        files={"file": ("p.jpg", b"x" * (1024 * 1024 + 1), "image/jpeg")},
        headers={"Authorization": "Bearer sess"},
    )
    assert oversize.status_code == 400
    empty = client.post(
        "/api/v1/uploads",
        files={"file": ("p.jpg", b"", "image/jpeg")},
        headers={"Authorization": "Bearer sess"},
    )
    assert empty.status_code == 400


def test_endpoint_serve_requires_auth(token, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {}})

    client = _app(token, handler, monkeypatch)
    no_auth = client.get("/api/v1/uploads/serve", params={"rel": "gezhi/short/u1/p.jpg"})
    assert no_auth.status_code == 401
