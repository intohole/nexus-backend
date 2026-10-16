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
    """发送通知，几乎不抛异常。返回 dict（检查 result["status"]：sent/suppressed/failed）
    或 None（调用层异常）。仅 status=="sent" 表示已送达；网络失败返回 failed dict 而非 None。
    判定送达请写 `result and result.get("status") == "sent"`，不要写 `result is None`。
    """
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


DEFAULT_TASK_FAILURE_RULES: list[tuple[str, str]] = [
    ("timeout", "生成超时（模型繁忙或网络波动），重试通常可过"),
    ("circuit", "AI 网关熔断保护中，稍等片刻重试"),
    ("rate", "触发频率保护，稍等片刻重试"),
    ("429", "触发频率保护，稍等片刻重试"),
    ("json", "AI 输出格式异常，重试会换一种写法"),
    ("解析", "AI 输出格式异常，重试会换一种写法"),
    ("重启", "服务重启导致任务中断，点开直接重新生成"),
]


def friendly_task_failure(error_message: str, extra_rules: Optional[list[tuple[str, str]]] = None) -> str:
    """技术错误 → 作者/用户语言（1.83.0 收编 verseCraft task_notify 规则表）。

    长任务失败文案的通用映射：错误码必须落到「用户下一步该做什么」；
    未命中规则时截断原文前 80 字（未知原因如实呈现，不编造）。
    """
    text = error_message or ""
    lowered = text.lower()
    for needle, friendly in list(extra_rules or []) + DEFAULT_TASK_FAILURE_RULES:
        if needle.lower() in lowered:
            return friendly
    return text[:80] or "未知原因"


async def notify_task_result(
    user_id: str,
    *,
    app_id: str,
    ok: bool,
    title: str,
    content: str = "",
    link: str = "",
    type: str = "task",
    priority: Optional[int] = None,
) -> Optional[dict[str, object]]:
    """任务终态触达样板（1.83.0）：成功/失败统一走 try_send 并按终态契约留痕。

    记账纪律（教训#294 生态化）：只有 status=="sent" 记「已投递」；
    suppressed/failed 与 None 都按未送达 WARNING（suppressed 属用户侧
    主动静默，语义不同但都不是送达）。priority 默认成功 1 / 失败 2。
    """
    try:
        result = await try_send(
            user_id=str(user_id),
            title=str(title)[:200],
            content=str(content)[:500],
            type=type,
            priority=priority if priority is not None else (1 if ok else 2),
            app_id=app_id,
            link=link,
        )
    except Exception as exc:
        logger.warning("任务终态通知投递异常: app=%s ok=%s err=%s", app_id, ok, exc)
        return None
    status = result.get("status") if isinstance(result, dict) else None
    if status == "sent":
        logger.info("任务终态通知已投递: app=%s ok=%s", app_id, ok)
    else:
        logger.warning(
            "任务终态通知未送达: app=%s ok=%s status=%s resp=%s",
            app_id, ok, status, str(result)[:120],
        )
    return result
