"""万象文件中台客户端:统一文件上传/图片变体/签名 URL 重签,供各应用复用。

URL 是租约不是产权:应用侧只持久化 rel key,渲染前用 sign_urls 换取新签名 URL。

r208 起含接入方资产网关层(AssetGateway + make_uploads_router):签名缓存、
rel 校验、流式回源与 uploads 端点族单点维护,接入方一行接线。
"""
from __future__ import annotations

import os
import time
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from nexus.auth import get_auth_deps
from nexus.discovery import get_registry, resolve_cached
from nexus.logging import get_logger
from nexus.service_client import get_service_token
from nexus.trace import outbound_headers

logger = get_logger("nexus.wanxiang")

def get_wanxiang_base_url() -> str:
    env_url = os.environ.get("WANXIANG_BASE_URL", "").rstrip("/")
    if env_url:
        return env_url
    cached = resolve_cached("wanxiang")
    if cached:
        return cached
    return get_registry().bootstrap("wanxiang")


class WanxiangError(RuntimeError):
    """万象上传/重签失败。调用方决定降级策略,本原语不静默吞错。"""


class WanxiangClient:

    def __init__(self, base_url: str = "", timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = (base_url or get_wanxiang_base_url()).rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def upload(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        variants: str = "",
        alias: str = "",
    ) -> dict:
        """上传文件,返回 {id, rel, url, expires_at, alias, ...}。应用应持久化 rel 而非 url。

        分发型公共资产(安装包/对外下载物)传 alias(如 "runner/win"):资产归入应用级
        命名空间,重传同 alias 即发布新版本,消费方经 resolve_alias 按名取最新——
        不要在代码里硬编码 rel,那会让每次换包都变成跨仓发版。"""
        token = await get_service_token()
        headers = {
            "X-Service-Token": token,
            "X-App-Name": app_name,
            "X-User-Id": user_id or "anonymous",
        }
        data: dict[str, str] = {}
        if variants:
            data["variants"] = variants
        if alias:
            data["alias"] = alias
        files = {"file": (filename or "file", content, mime_type or "application/octet-stream")}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(f"{self._base_url}/api/v1/assets/upload", headers=headers, files=files, data=data)
        if resp.status_code != 200:
            detail = _detail(resp)
            logger.warning("wanxiang upload failed: %s %s", resp.status_code, detail)
            raise WanxiangError(f"万象上传失败: {detail}")
        body = resp.json()
        if not body.get("success") or not isinstance(body.get("data"), dict):
            raise WanxiangError("万象上传返回异常")
        return body["data"]

    async def resolve_alias(self, app_name: str, alias: str) -> dict:
        """按别名取当前版本元数据+新签名 URL(分发动线唯一需要的读原语)。

        返回 {id, rel, url, expires_at, filename, sha256, ...};别名不存在抛 WanxiangError。
        调用方对未知别名自行决定回退(如 goldenFish 回退存量硬编码 rel)。"""
        token = await get_service_token()
        headers = {"X-Service-Token": token, "X-App-Name": app_name}
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.get(
                f"{self._base_url}/api/v1/assets/resolve",
                headers=headers,
                params={"alias": alias},
            )
        if resp.status_code == 404:
            raise WanxiangError(f"万象别名不存在: {alias}")
        if resp.status_code != 200:
            detail = _detail(resp)
            logger.warning("wanxiang resolve failed: %s %s", resp.status_code, detail)
            raise WanxiangError(f"万象别名解析失败: {detail}")
        body = resp.json()
        if not body.get("success") or not isinstance(body.get("data"), dict):
            raise WanxiangError("万象别名解析返回异常")
        return body["data"]

    async def sign_urls(self, app_name: str, rels: list[str], days: int | None = None) -> dict:
        """按 rel 批量重签(单次上限 100),返回 {rel: {url, expires_at}}。

        days=None 走服务端按资产类别的默认租约;显式传入(1~365)覆盖默认——
        嵌入静态产物(PDF/HTML 导出)的场景必须用长租约,产物寿命与租约对齐,
        否则产物被再次打开时图片恒 403。
        """
        if not rels:
            return {}
        token = await get_service_token()
        headers = {"X-Service-Token": token, "X-App-Name": app_name, "Content-Type": "application/json"}
        payload: dict = {"rels": rels[:100]}
        if days is not None:
            payload["days"] = int(days)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(f"{self._base_url}/api/v1/assets/sign", headers=headers, json=payload)
        if resp.status_code != 200:
            detail = _detail(resp)
            logger.warning("wanxiang sign failed: %s %s", resp.status_code, detail)
            raise WanxiangError(f"万象重签失败: {detail}")
        body = resp.json()
        if not body.get("success") or not isinstance(body.get("data"), dict):
            raise WanxiangError("万象重签返回异常")
        return body["data"]


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:200] or resp.reason_phrase
    return str(body.get("detail") or body)[:200]


_wanxiang_instance: Optional[WanxiangClient] = None


def get_wanxiang_client() -> WanxiangClient:
    global _wanxiang_instance
    if _wanxiang_instance is None:
        _wanxiang_instance = WanxiangClient()
    return _wanxiang_instance


class AssetGateway:
    """接入方资产网关:签名缓存+rel 校验+流式回源单点。

    r208 自 gezhi/xunji 各自手搓的 AssetService 薄封装收归(逐字节克隆判例)。
    签名租约 TTL 60s 进程内缓存,容量超限全清;rel 校验(段数/应用前缀/穿越)
    是安全面,收归单点防双份漂移。方法抛 HTTPException:与历史接入方行为
    逐字节一致,端点与非 HTTP 语境(如 vision 链读图字节)均可直接消费。
    """

    SIGN_TTL = 60.0
    CACHE_CAP = 2000
    ALLOWED_IMAGE_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

    def __init__(
        self,
        app_name: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.app_name = app_name
        self._timeout = timeout
        self._transport = transport
        self._client_override: WanxiangClient | None = None
        self._sign_cache: dict[str, tuple[str, float]] = {}

    async def upload(self, user_id: str, filename: str, content: bytes, mime_type: str) -> str:
        try:
            meta = await self._client().upload(
                app_name=self.app_name,
                user_id=user_id,
                filename=filename,
                content=content,
                mime_type=mime_type,
                variants="binary",
            )
        except WanxiangError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        rel = meta.get("rel") or ""
        if not rel:
            raise HTTPException(status_code=502, detail="万象返回缺少 rel")
        return rel

    def _client(self) -> WanxiangClient:
        if self._client_override is None:
            if self._transport is not None or self._timeout != 30.0:
                self._client_override = WanxiangClient(
                    base_url=get_wanxiang_base_url(), timeout=self._timeout, transport=self._transport
                )
            else:
                self._client_override = get_wanxiang_client()
        return self._client_override

    async def _signed_query(self, rel: str) -> str:
        cached = self._sign_cache.get(rel)
        if cached and cached[1] > time.time():
            return cached[0]
        data = await self._client().sign_urls(self.app_name, [rel])
        entry = data.get(rel)
        if not entry:
            raise HTTPException(status_code=404, detail="文件不存在")
        url = entry["url"]
        qs = url.split("?", 1)[1] if "?" in url else ""
        if not qs:
            raise HTTPException(status_code=502, detail="万象签名返回异常")
        if len(self._sign_cache) > self.CACHE_CAP:
            self._sign_cache.clear()
        self._sign_cache[rel] = (qs, time.time() + self.SIGN_TTL)
        return qs

    async def open_stream(self, rel: str) -> tuple[httpx.AsyncClient, httpx.Response]:
        if not rel or rel.count("/") != 3 or not rel.startswith(f"{self.app_name}/") or ".." in rel:
            raise HTTPException(status_code=400, detail="资源标识无效")
        qs = await self._signed_query(rel)
        internal = f"{get_wanxiang_base_url()}/api/v1/assets/serve/{rel}?{qs}"
        client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        req = client.build_request("GET", internal, headers=outbound_headers())
        try:
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail="文件分发失败") from exc
        if upstream.status_code == 404:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(status_code=404, detail="文件不存在")
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="文件分发失败")
        return client, upstream

    async def read_bytes(self, rel: str) -> bytes:
        client, upstream = await self.open_stream(rel)
        try:
            return await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()


_gateways: dict[str, AssetGateway] = {}


def get_asset_gateway(app_name: str) -> AssetGateway:
    gateway = _gateways.get(app_name)
    if gateway is None:
        gateway = AssetGateway(app_name)
        _gateways[app_name] = gateway
    return gateway


def make_uploads_router(
    *,
    app_name: str,
    get_current_uid: Callable[..., Awaitable[str]],
    max_image_mb: float = 10.0,
    api_prefix: str = "/api/v1",
    tags: list[str] | None = None,
) -> APIRouter:
    """uploads 端点族工厂:POST /uploads 上传 + GET /uploads/serve 鉴权流式回源。

    接入方一行 `api_router.include_router(make_uploads_router(app_name=...))`
    直替原手搓 uploads.py(r208);鉴权依赖由接入方传入(上传走会话鉴权,
    serve 额外收 ?t=<jwt> 兜位——img 标签裸请求带不了 Authorization)。
    响应 {url, rel}:url 供前端即用,rel 供业务持久化(产权键)。
    """
    gateway = get_asset_gateway(app_name)
    router = APIRouter(prefix="/uploads", tags=tags or ["附件"])

    async def _uid_from_header_or_query(request: Request, t: str = "") -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:].strip())
        elif t:
            creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=t.strip())
        else:
            raise HTTPException(status_code=401, detail="未登录")
        return await get_auth_deps().get_user_id_required(creds)

    @router.post("")
    async def upload_image(
        file: UploadFile,
        user_id: str = Depends(get_current_uid),
    ) -> dict[str, object]:
        mime = (file.content_type or "").lower()
        if mime not in AssetGateway.ALLOWED_IMAGE_MIME:
            raise HTTPException(status_code=400, detail="仅支持 JPG/PNG/WebP 图片")
        limit = int(max_image_mb * 1024 * 1024)
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="文件为空")
        if len(raw) > limit:
            raise HTTPException(status_code=400, detail="图片超过大小限制")
        rel = await gateway.upload(
            user_id=user_id,
            filename=file.filename or f"photo.{AssetGateway.ALLOWED_IMAGE_MIME[mime]}",
            content=raw,
            mime_type=mime,
        )
        return {"url": f"{api_prefix}/uploads/serve?rel={rel}", "rel": rel}

    @router.get("/serve")
    async def serve(
        request: Request,
        rel: str = "",
        t: str = "",
    ) -> StreamingResponse:
        user_id = await _uid_from_header_or_query(request, t)
        _ = user_id
        client, upstream = await gateway.open_stream(rel)

        async def _iter():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            _iter(),
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=60"},
        )

    return router
