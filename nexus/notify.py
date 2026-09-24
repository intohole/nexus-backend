"""通知统一出口：站内/邮件等通知发送与 notifyCenter 客户端管理。"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response

from nexus.defaults import DEFAULT_NOTIFY_CENTER_URL
from nexus.infra import get_notify_center_url
from nexus.logging import get_logger
from nexus.service_client import get_service_token
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
        self._service_token: str = service_token
        self._timeout: float = timeout
        self._http: HttpClient = HttpClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _headers(self) -> dict[str, str]:
        token: str = self._service_token or await get_service_token()
        return {"X-Service-Token": token} if token else {}

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
                headers=await self._headers(),
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


async def send_webhook_robot(
    channel: str,
    webhook_url: str,
    title: str,
    content: str,
    level: str = "info",
    *,
    api_key: str = "",
    chat_id: str = "",
) -> bool:
    """群机器人 webhook 告警发送：支持 wechat / dingtalk / telegram / bark。

    统一 httpx 发送与结果判定，吸收各项目自建渠道样板。
    """
    import httpx as _httpx

    try:
        if channel == "wechat":
            if not webhook_url:
                return False
            payload: dict[str, object] = {
                "msgtype": "markdown",
                "markdown": {"content": f"### {title}\n\n{content}\n\n> 级别: {level}"},
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                return resp.json().get("errcode", -1) == 0
        elif channel == "dingtalk":
            if not webhook_url:
                return False
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": f"### {title}\n\n{content}\n\n> 级别: {level}"},
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook_url, json=payload)
                return resp.json().get("errcode", -1) == 0
        elif channel == "telegram":
            if not api_key or not chat_id:
                return False
            url = f"https://api.telegram.org/bot{api_key}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": f"*{title}*\n\n{content}\n\n_级别: {level}_",
                "parse_mode": "Markdown",
            }
            async with _httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                return resp.json().get("ok", False)
        elif channel == "bark":
            if not api_key:
                return False
            url = f"https://api.day.app/{api_key}/{title}/{content}"
            async with _httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                return resp.json().get("code", -1) == 200
        return False
    except Exception:
        return False


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
