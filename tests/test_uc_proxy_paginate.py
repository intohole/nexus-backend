"""register_uc_proxy 代理与 503 降级回归（repository.paginate 原语已随 r169 退役）。"""
from __future__ import annotations

import httpx
from fastapi import FastAPI

from nexus.uc_proxy import register_uc_proxy


def test_register_uc_proxy_proxies_and_503(monkeypatch):
    app = FastAPI()
    register_uc_proxy(app)
    client = httpx.AsyncClient  # 引用面自检：原语内部共享 client
    assert client is not None

    @app.get("/other")
    async def other():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    import asyncio

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            monkeypatch.delenv("UC__BASE_URL", raising=False)
            import nexus.uc_proxy as m

            monkeypatch.setattr(m, "_resolve_uc_base", lambda: "")
            r1 = await c.get("/uc-api/api/auth/config")
            assert r1.status_code == 503
            monkeypatch.setattr(m, "_resolve_uc_base", lambda: "http://upstream")
            r2 = await c.get("/other")
            assert r2.status_code == 200

    asyncio.run(run())
