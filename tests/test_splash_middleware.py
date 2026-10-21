from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

from nexus.middleware import LoadingSplashMiddleware
from nexus.middleware_splash import build_splash


def build_client(app_name: str = "测试应用") -> TestClient:
    app = FastAPI()
    app.add_middleware(LoadingSplashMiddleware, app_name=app_name)

    @app.get("/page", response_class=HTMLResponse)
    async def page() -> str:
        return "<!DOCTYPE html><html><body><div id=\"app\"></div></body></html>"

    @app.get("/api/data")
    async def data() -> JSONResponse:
        return JSONResponse({"ok": True})

    return TestClient(app)


def test_splash_template_session_adaptive_min():
    html = build_splash("星轨挑战")
    assert "__nexus_splash_seen__" in html
    assert "280" in html and "1100" in html
    assert "minShow" in html
    assert html.index("280") < html.index("1100")
    assert html.count("sessionStorage") >= 2


def test_splash_injected_into_html_only():
    client = build_client()
    resp = client.get("/page")
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "__nexus_splash__" in body
    assert body.index("<body") < body.index("__nexus_splash__")
    declared = int(resp.headers["content-length"])
    assert declared == len(resp.content)


def test_splash_not_injected_into_json():
    client = build_client()
    resp = client.get("/api/data")
    assert resp.json() == {"ok": True}
    assert "__nexus_splash__" not in resp.text
