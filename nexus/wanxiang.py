"""万象文件中台客户端:统一文件上传/图片变体/签名 URL 重签,供各应用复用。

URL 是租约不是产权:应用侧只持久化 rel key,渲染前用 sign_urls 换取新签名 URL。
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

from nexus.logging import get_logger
from nexus.service_client import get_service_token

logger = get_logger("nexus.wanxiang")

DEFAULT_BASE_URL = "http://10.100.0.4:8001"


def get_wanxiang_base_url() -> str:
    return os.environ.get("WANXIANG_BASE_URL", "").rstrip("/") or DEFAULT_BASE_URL


class WanxiangError(RuntimeError):
    """万象上传/重签失败。调用方决定降级策略,本原语不静默吞错。"""


class WanxiangClient:

    def __init__(self, base_url: str = "", timeout: float = 30.0):
        self._base_url = (base_url or get_wanxiang_base_url()).rstrip("/")
        self._timeout = timeout

    async def upload(
        self,
        app_name: str,
        user_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        variants: str = "",
    ) -> dict:
        """上传文件,返回 {id, rel, url, expires_at, ...}。应用应持久化 rel 而非 url。"""
        token = await get_service_token()
        headers = {
            "X-Service-Token": token,
            "X-App-Name": app_name,
            "X-User-Id": user_id or "anonymous",
        }
        data: dict[str, str] = {}
        if variants:
            data["variants"] = variants
        files = {"file": (filename or "file", content, mime_type or "application/octet-stream")}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/api/v1/assets/upload", headers=headers, files=files, data=data)
        if resp.status_code != 200:
            detail = _detail(resp)
            logger.warning("wanxiang upload failed: %s %s", resp.status_code, detail)
            raise WanxiangError(f"万象上传失败: {detail}")
        body = resp.json()
        if not body.get("success") or not isinstance(body.get("data"), dict):
            raise WanxiangError("万象上传返回异常")
        return body["data"]

    async def sign_urls(self, app_name: str, rels: list[str]) -> dict:
        """按 rel 批量重签(单次上限 100),返回 {rel: {url, expires_at}}。"""
        if not rels:
            return {}
        token = await get_service_token()
        headers = {"X-Service-Token": token, "X-App-Name": app_name, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/api/v1/assets/sign", headers=headers, json={"rels": rels[:100]})
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
