"""万象文件中台客户端:统一文件上传/图片变体/签名 URL 重签,供各应用复用。

URL 是租约不是产权:应用侧只持久化 rel key,渲染前用 sign_urls 换取新签名 URL。
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from nexus.discovery import get_registry, resolve_cached
from nexus.logging import get_logger
from nexus.service_client import get_service_token

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
