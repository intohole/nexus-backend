"""通知反代注册：把 notifyCenter API 挂到业务 FastAPI 的 /api/notify 路径。"""
from __future__ import annotations

def register_notify_proxy(app: FastAPI) -> None:
    """在任意 FastAPI app 上注册 /api/notify/* 反向代理到 notifyCenter。

    前端通过项目后端代理访问 notifyCenter，避免跨域和鉴权问题。
    目标地址经 Lion infra 权威解析（与 async_init_notify_client 同源），
    Lion 不可达时回退 NOTIFY_CENTER_URL 环境变量或 defaults 默认值。
    """
    proxy_client = httpx.AsyncClient(timeout=30.0)

    @app.api_route("/api/notify/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def notify_proxy(path: str, request: Request) -> Response:
        base_url: str = await get_notify_center_url()
        target_url: str = f"{base_url}/api/notify/{path}"
        query: str = request.url.query
        if query:
            target_url += f"?{query}"
        req_headers: dict[str, str] = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "accept-encoding")
        }
        resp: httpx.Response = await proxy_client.request(
            method=request.method,
            url=target_url,
            content=await request.body(),
            headers=req_headers,
        )
        resp_headers: dict[str, str] = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
        )


__all__ = [
    "NotifyClient",
    "get_notify_client",
    "async_init_notify_client",
    "send_notification",
    "send_email",
    "send_admin_email",
    "send_sms",
    "register_notify_proxy",
]
