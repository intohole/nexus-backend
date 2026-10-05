"""UC 反向代理注册：把 usercenter API 挂到业务 FastAPI 的 /uc-api 路径。

前端通过项目后端代理访问 usercenter，避免跨域；SDK baseUrl 指向 /uc-api。
目标地址解析顺序：UC_BASE_URL 环境变量 > UC SDK 实际 base_url（与业务侧
手写版逐字等价）。未解析到时返回 503，由前端提示 usercenter 未配置。
"""
from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


def _resolve_uc_base() -> str:
    from nexus.uc_sdk_helper import get_uc_sdk

    return (
        os.getenv("UC_BASE_URL")
        or getattr(get_uc_sdk(), "base_url", "")
        or ""
    ).rstrip("/")


def register_uc_proxy(app: FastAPI, prefix: str = "/uc-api") -> None:
    """在任意 FastAPI app 上注册 {prefix}/{path} 反向代理到 usercenter。"""
    proxy_client = httpx.AsyncClient(timeout=30.0)

    @app.api_route(
        f"{prefix}/{{path:path}}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def uc_proxy(path: str, request: Request) -> Response:
        base: str = _resolve_uc_base()
        if not base:
            return JSONResponse({"error": "usercenter 未配置"}, status_code=503)
        req_headers: dict[str, str] = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "accept-encoding")
        }
        resp: httpx.Response = await proxy_client.request(
            method=request.method,
            url=f"{base}/{path}",
            headers=req_headers,
            content=await request.body(),
            params=request.query_params,
        )
        excluded = {"content-encoding", "transfer-encoding", "content-length", "connection"}
        resp_headers: dict[str, str] = {
            k: v for k, v in resp.headers.items() if k.lower() not in excluded
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
        )
