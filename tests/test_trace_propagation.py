"""S34 跨服务调用链透传守底：出站头注入/透传沿用/缺席生成/零漂移四断言族。"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus import TRACE_HEADER, ensure_request_id, outbound_headers
from nexus.context import get_request_id, set_request_context


@pytest.fixture(autouse=True)
def _clear_request_context():
    set_request_context(request_id="")
    yield
    set_request_context(request_id="")


class TestTracePrimitive:
    def test_outbound_headers_always_carries_trace_header(self):
        headers = outbound_headers({"Authorization": "Bearer x"})
        assert TRACE_HEADER in headers and headers[TRACE_HEADER]
        assert headers["Authorization"] == "Bearer x"

    def test_ensure_request_id_generates_and_backfills(self):
        assert get_request_id() == ""
        rid = ensure_request_id()
        assert rid and get_request_id() == rid

    def test_ensure_request_id_reuses_existing(self):
        set_request_context(request_id="fixed-rid-1")
        assert ensure_request_id() == "fixed-rid-1"

    def test_header_name_matches_inbound_middleware(self):
        from nexus.middleware_base import REQUEST_ID_HEADER

        assert TRACE_HEADER == REQUEST_ID_HEADER == "X-Request-ID"


class TestSdkBaseInjection:
    def test_base_client_request_carries_trace_header(self, monkeypatch):
        from nexus.sdk_base import BaseAsyncClient

        captured: dict[str, object] = {}

        class FakeClient(BaseAsyncClient):
            service_name = "fake"

            async def _headers(self):
                return {"X-Service-Token": "tok"}

        client = FakeClient("http://127.0.0.1:1")

        async def fake_request(method, path, *, headers=None, **kwargs):
            captured["headers"] = headers
            resp = httpx.Response(200, json={"ok": True}, request=httpx.Request(method, path))
            return resp

        async def fake_get_client():
            class C:
                request = staticmethod(fake_request)

            return C()

        monkeypatch.setattr(client, "_get_client", fake_get_client)
        set_request_context(request_id="sdk-rid-9")
        asyncio.run(client._request("GET", "/ping"))
        assert captured["headers"][TRACE_HEADER] == "sdk-rid-9"
        assert captured["headers"]["X-Service-Token"] == "tok"


class TestHttpClientInjection:
    def test_http_client_request_injects_and_respects_existing(self):
        from nexus.utils.http import HttpClient

        captured: dict[str, object] = {}

        hc = HttpClient(base_url="http://127.0.0.1:1")

        class FakeAsyncClient:
            async def request(self, method, url, **kwargs):
                captured["headers"] = kwargs.get("headers")
                return httpx.Response(200, request=httpx.Request(method, url))

        async def run():
            hc._client = FakeAsyncClient()

            async def noop_init():
                return None

            hc.init = noop_init  # type: ignore[method-assign]
            set_request_context(request_id="hc-rid-7")
            await hc.get("/x")
            assert captured["headers"][TRACE_HEADER] == "hc-rid-7"

            await hc.post("/y", headers={"X-Custom": "c", TRACE_HEADER: "caller-rid"})
            assert captured["headers"][TRACE_HEADER] == "caller-rid"
            assert captured["headers"]["X-Custom"] == "c"

        asyncio.run(run())


class TestProxyPassthrough:
    def _install_fake_proxy_client(self, app, path_pattern: str) -> dict[str, object]:
        forwarded: dict[str, object] = {}

        class FakeProxyClient:
            async def request(self, method, url, **kwargs):
                forwarded["headers"] = kwargs.get("headers")
                forwarded["url"] = url
                return httpx.Response(200, json={"ok": 1})

        replaced = False
        for route in app.routes:
            if getattr(route, "path", "") == path_pattern:
                endpoint = route.endpoint
                closure = getattr(endpoint, "__closure__", None) or ()
                for cell in closure:
                    try:
                        if isinstance(cell.cell_contents, httpx.AsyncClient):
                            cell.cell_contents = FakeProxyClient()
                            replaced = True
                    except ValueError:
                        pass
        assert replaced, f"proxy_client closure cell not found for {path_pattern}"
        return forwarded

    def test_notify_proxy_forwards_inbound_id(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import nexus.notify_proxy as np_mod

        app = FastAPI()
        np_mod.register_notify_proxy(app)
        forwarded = self._install_fake_proxy_client(app, "/api/notify/{path:path}")

        client = TestClient(app)
        resp = client.request("POST", "/api/notify/send", json={"a": 1},
                              headers={"X-Request-ID": "proxy-rid-5"})
        assert resp.status_code == 200
        assert forwarded["headers"].get("X-Request-ID") == "proxy-rid-5"

    def test_uc_proxy_forwards_inbound_id(self):
        import os

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import nexus.uc_proxy as uc_mod

        os.environ.setdefault("UC__BASE_URL", "http://127.0.0.1:8901")
        app = FastAPI()
        uc_mod.register_uc_proxy(app)
        forwarded = self._install_fake_proxy_client(app, "/uc-api/{path:path}")

        client = TestClient(app)
        resp = client.request("GET", "/uc-api/me", headers={"X-Request-ID": "ucproxy-rid-3"})
        assert resp.status_code == 200
        assert forwarded["headers"].get("X-Request-ID") == "ucproxy-rid-3"

    def test_proxy_generates_id_when_absent(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import nexus.notify_proxy as np_mod

        app = FastAPI()
        np_mod.register_notify_proxy(app)
        forwarded = self._install_fake_proxy_client(app, "/api/notify/{path:path}")

        client = TestClient(app)
        resp = client.request("GET", "/api/notify/inbox")
        assert resp.status_code == 200
        rid = forwarded["headers"].get("X-Request-ID")
        assert rid and isinstance(rid, str) and len(rid) > 0


class TestInboundReuseSemantics:
    def test_upstream_header_reused_absent_generated(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from nexus.fastapi_setup import setup_middleware

        app = FastAPI()
        setup_middleware(app, enable_cors=False, enable_rate_limit=False,
                         enable_no_cache=False)

        @app.get("/echo-rid")
        async def echo_rid():
            from nexus.context import get_request_id

            return {"rid": get_request_id()}

        client = TestClient(app)
        r1 = client.get("/echo-rid", headers={"X-Request-ID": "reuse-me"})
        assert r1.json()["rid"] == "reuse-me"
        assert r1.headers["X-Request-ID"] == "reuse-me"
        r2 = client.get("/echo-rid")
        assert r2.json()["rid"] and r2.json()["rid"] != "reuse-me"
