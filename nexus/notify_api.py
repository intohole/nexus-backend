"""通知便捷函数层：模块级 send_* 一行直达（委托 NotifyClient 单例）。

send_notification 返回统一终态契约（1.57.0 起）：
- {"status": "sent", "id": N, ...}       落库且渠道已派发
- {"status": "suppressed", "id": 0, "reason": "app_muted|daily_limit|...", ...}
- {"status": "failed", "id": 0, "reason": "http_4xx|network"}
向后兼容：id/suppressed/deduped/channels_sent 字段保持原语义。

try_send（1.59.0 起）：send 的安全壳——NotifyClient.send 已内吞网络/HTTP 异常
返回 failed dict，业务侧的 try/except 包装是死铠甲；本壳只兜 get_notify_client
初始化等边缘异常，统一告警后返回 None。调用方「只关心送达没有」用
`resp is not None and resp.get("status") == "sent"` 判定。
"""
from __future__ import annotations

from typing import Optional

from nexus.logging import get_logger
from nexus.notify import NotifyClient, get_notify_client

logger = get_logger("nexus.notify_api")

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


async def try_send(
    user_id: str,
    title: str,
    content: str = "",
    **kwargs: object,
) -> Optional[dict[str, object]]:
    from nexus.notify import get_notify_client  # 调用时解析：monkeypatch nexus.notify.get_notify_client 的测试桩保持有效

    try:
        return await get_notify_client().send(
            user_id=user_id, title=title, content=content, **kwargs
        )
    except Exception as exc:
        logger.warning(
            "notify try_send failed: user=%s title=%s err=%s",
            user_id, str(title)[:40], exc,
        )
        return None


async def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    html: str | None = None,
) -> bool:
    client: NotifyClient = get_notify_client()
    return await client.send_email(to=to, subject=subject, body=body, html=html)


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
        async with _httpx.AsyncClient(timeout=10.0) as client:
            if channel == "wechat":
                if not webhook_url:
                    return False
                resp = await client.post(webhook_url, json={
                    "msgtype": "markdown",
                    "markdown": {"content": f"### {title}\n\n{content}\n\n> 级别: {level}"},
                })
                return resp.json().get("errcode", -1) == 0
            elif channel == "dingtalk":
                if not webhook_url:
                    return False
                resp = await client.post(webhook_url, json={
                    "msgtype": "markdown",
                    "markdown": {"title": title, "text": f"### {title}\n\n{content}\n\n> 级别: {level}"},
                })
                return resp.json().get("errcode", -1) == 0
            elif channel == "telegram":
                if not api_key or not chat_id:
                    return False
                resp = await client.post(f"https://api.telegram.org/bot{api_key}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": f"*{title}*\n\n{content}\n\n_级别: {level}_",
                    "parse_mode": "Markdown",
                })
                return resp.json().get("ok", False)
            elif channel == "bark":
                if not api_key:
                    return False
                resp = await client.get(f"https://api.day.app/{api_key}/{title}/{content}")
                return resp.json().get("code", -1) == 200
        return False
    except Exception:
        return False
