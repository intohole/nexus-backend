from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

from nexus.defaults import DEFAULT_NOTIFY_CENTER_URL
from nexus.infra import get_notify_center_url
from nexus.logging import get_logger
from nexus.utils import HttpClient

logger = get_logger("nexus.notify")


class NotifyClient:
    def __init__(
        self,
        base_url: str = "",
        service_token: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.environ.get("NOTIFY_CENTER_URL", DEFAULT_NOTIFY_CENTER_URL)
        )
        self._service_token: str = service_token or os.environ.get(
            "SERVICE_TOKEN", ""
        )
        self._timeout: float = timeout
        self._http: HttpClient = HttpClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-Service-Token": self._service_token},
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def send(
        self,
        user_id: str,
        title: str,
        content: str = "",
        type: str = "system",
        priority: int = 1,
        app_id: str = "system",
        data: Optional[dict[str, object]] = None,
        link: str = "",
        channels: Optional[list[str]] = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "user_id": user_id,
            "app_id": app_id,
            "type": type,
            "priority": priority,
            "title": title,
            "content": content,
            "data": data or {},
            "link": link,
            "channels": channels or ["in_app"],
        }
        try:
            resp: httpx.Response = await self._http.post(
                "/api/notify/send",
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Notify send failed: status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return {}
        except Exception as exc:
            logger.error("Notify send error: %s", str(exc))
            return {}

    async def send_many(
        self,
        items: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for item in items:
            try:
                result: dict[str, object] = await self.send(**item)
            except Exception as exc:
                logger.error("Batch notify item failed: %s", str(exc))
                result = {}
            results.append(result)
        return results

    async def send_admin_email(
        self,
        subject: str,
        content: str = "",
        email: str = "",
        priority: int = 3,
        app_id: str = "system",
        type: str = "alert",
    ) -> dict[str, object]:
        admin_email: str = email or os.environ.get("ADMIN_NOTIFY_EMAIL", "")
        if not admin_email:
            logger.warning("ADMIN_NOTIFY_EMAIL未配置，跳过管理员邮件通知")
            return {}
        return await self.send(
            user_id="admin",
            title=subject,
            content=content,
            type=type,
            priority=priority,
            app_id=app_id,
            channels=["email"],
            data={"email": admin_email},
        )

    async def send_email_via_center(
        self,
        to: str,
        subject: str,
        body: str,
        html: str | None = None,
    ) -> bool:
        try:
            resp: httpx.Response = await self._http.post(
                "/api/notify/email",
                json={
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "html": html or "",
                },
            )
            resp.raise_for_status()
            data: dict[str, object] = resp.json()
            return bool(data.get("sent", False))
        except Exception as exc:
            logger.error("NotifyCenter email send failed: %s", str(exc))
            return False

    async def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        html: str | None = None,
        from_addr: str = "",
    ) -> bool:
        recipients: list[str] = [to] if isinstance(to, str) else list(to)
        if len(recipients) == 1:
            sent_center: bool = await self.send_email_via_center(
                to=recipients[0], subject=subject, body=body, html=html
            )
            if sent_center:
                return True
        sent: bool = False
        for recipient in recipients:
            ok: bool = await self.send_email_via_center(
                to=recipient, subject=subject, body=body, html=html
            )
            sent = sent or ok
        return sent

    async def send_sms(
        self,
        phone: str,
        template_code: str,
        template_param: Optional[dict[str, object]] = None,
        sign_name: str = "",
    ) -> bool:
        try:
            resp: httpx.Response = await self._http.post(
                "/api/notify/sms",
                json={
                    "phone": phone,
                    "template_code": template_code,
                    "template_param": template_param or {},
                    "sign_name": sign_name,
                },
            )
            resp.raise_for_status()
            data: dict[str, object] = resp.json()
            return bool(data.get("sent", False))
        except Exception as exc:
            logger.error("NotifyCenter SMS send failed: %s", str(exc))
            return False

    async def close(self) -> None:
        await self._http.close()


_notify_client: Optional[NotifyClient] = None
_notify_client_initialized: bool = False


async def async_init_notify_client() -> None:
    global _notify_client, _notify_client_initialized
    if _notify_client_initialized:
        return
    try:
        base_url = await get_notify_center_url()
        if base_url:
            _notify_client = NotifyClient(base_url=base_url)
            _notify_client_initialized = True
            logger.info("NotifyClient initialized from Lion infra: %s", base_url)
            return
    except Exception as e:
        logger.warning("Failed to init NotifyClient from Lion infra: %s", e)
    _notify_client = NotifyClient()
    _notify_client_initialized = True


def get_notify_client(base_url: str = "") -> NotifyClient:
    global _notify_client
    if _notify_client is None:
        _notify_client = NotifyClient(base_url=base_url)
    return _notify_client


async def send_notification(
    user_id: str,
    title: str,
    content: str = "",
    **kwargs: object,
) -> dict[str, object]:
    client: NotifyClient = get_notify_client()
    return await client.send(
        user_id=user_id, title=title, content=content, **kwargs
    )


async def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    html: str | None = None,
    from_addr: str = "",
) -> bool:
    client: NotifyClient = get_notify_client()
    return await client.send_email(
        to=to, subject=subject, body=body, html=html, from_addr=from_addr
    )


async def send_admin_email(
    subject: str,
    content: str = "",
    email: str = "",
    priority: int = 3,
    app_id: str = "system",
    type: str = "alert",
) -> dict[str, object]:
    client: NotifyClient = get_notify_client()
    return await client.send_admin_email(
        subject=subject,
        content=content,
        email=email,
        priority=priority,
        app_id=app_id,
        type=type,
    )


async def send_sms(
    phone: str,
    template_code: str,
    template_param: Optional[dict[str, object]] = None,
    sign_name: str = "",
) -> bool:
    client: NotifyClient = get_notify_client()
    return await client.send_sms(
        phone=phone,
        template_code=template_code,
        template_param=template_param,
        sign_name=sign_name,
    )


def register_notify_proxy(app: FastAPI) -> None:
    """在任意 FastAPI app 上注册 /api/notify/* 反向代理到 notifyCenter。

    前端通过项目后端代理访问 notifyCenter，避免跨域和鉴权问题。
    目标地址从 NOTIFY_CENTER_URL 环境变量获取，默认走 nexus.defaults 单一权威源。
    """
    notify_center_url: str = os.environ.get("NOTIFY_CENTER_URL", DEFAULT_NOTIFY_CENTER_URL)

    @app.api_route("/api/notify/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def notify_proxy(path: str, request: Request) -> Response:
        target_url: str = f"{notify_center_url}/api/notify/{path}"
        query: str = request.url.query
        if query:
            target_url += f"?{query}"
        body: bytes = await request.body()
        headers: dict[str, str] = dict(request.headers)
        headers.pop("host", None)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp: httpx.Response = await client.request(
                method=request.method,
                url=target_url,
                content=body,
                headers=headers,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
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
