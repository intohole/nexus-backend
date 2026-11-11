"""通知统一出口：站内/邮件等通知发送与 notifyCenter 客户端管理。"""
from __future__ import annotations

import inspect
import os
import time
from typing import Optional

import httpx

from nexus.defaults import DEFAULT_NOTIFY_CENTER_URL
from nexus.infra import get_notify_center_url
from nexus.logging import get_logger
from nexus.notify_breaker import (
    BreakerOpenError, ConnectBreaker, note_exception,
)
from nexus.service_client import get_service_token
from nexus.utils import HttpClient

logger = get_logger("nexus.notify")


class NotifyClient:
    def __init__(
        self,
        base_url: str = "",
        service_token: str = "",
        timeout: float = 10.0,
        connect_timeout: float = 2.0,
    ) -> None:
        self._base_url: str = (
            base_url
            or os.environ.get("NOTIFY_CENTER_URL", DEFAULT_NOTIFY_CENTER_URL)
        )
        self._service_token: str = service_token
        self._timeout: float = timeout
        self._http: HttpClient = HttpClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
        )
        self._breaker = ConnectBreaker()

    async def _call(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """统一出网闸：open 期零网络快速失败；连接级失败喂断路器。"""
        if not self._breaker.allow(time.monotonic()):
            raise BreakerOpenError("notify_center_breaker_open")
        try:
            resp: httpx.Response = await getattr(self._http, method.lower())(path, **kwargs)
        except Exception as exc:
            note_exception(self._breaker, exc)
            raise
        self._breaker.on_success()
        return resp

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _headers(self) -> dict[str, str]:
        token: str = self._service_token or await get_service_token()
        return {"X-Service-Token": token} if token else {}

    async def _audience_blocked(self, user_id: str) -> bool:
        """出口受众净化：True=该用户不可触达（孤儿/测试/停用号），请求不出网。

        nexus.audience 缺席（旧 venv）或查询异常一律 fail-open——与 audience
        原语自身的 UC 不可达语义一致，不为净化引入新故障面。
        """
        try:
            from nexus.audience import sendable
        except ImportError:
            logger.warning("audience_check requested but nexus.audience unavailable — fail-open")
            return False
        try:
            return not await sendable(user_id)
        except Exception as exc:
            logger.warning("audience guard check failed (%s) — fail-open", exc)
            return False

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
        why_you: str = "",
        audience_check: bool = False,
        dedup_key: str = "",
    ) -> dict[str, object]:
        if audience_check and await self._audience_blocked(str(user_id)):
            logger.info(
                "Notify send suppressed by audience guard: user=%s app=%s title=%s",
                user_id, app_id, str(title)[:40],
            )
            return {"status": "suppressed", "id": 0, "reason": "audience_not_sendable"}
        merged_data: dict[str, object] = dict(data or {})
        if why_you and "why_you" not in merged_data:
            merged_data["why_you"] = why_you[:200]
        payload: dict[str, object] = {
            "user_id": user_id,
            "app_id": app_id,
            "type": type,
            "priority": priority,
            "title": title,
            "content": content,
            "data": merged_data,
            "link": link,
            "channels": channels or ["in_app"],
        }
        if dedup_key:
            # 调用方幂等键（r24 收归）：同键重发（重启补跑/影子实例）由 notifyCenter 吸收。
            payload["dedup_key"] = dedup_key[:200]
        try:
            resp: httpx.Response = await self._call("POST", "/api/notify/send",
                json=payload,
                headers=await self._headers(),
            )
            resp.raise_for_status()
            body: dict[str, object] = resp.json()
        except BreakerOpenError:
            return {"status": "failed", "id": 0, "reason": "notify_center_breaker_open"}
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Notify send failed: status=%s body=%s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return {
                "status": "failed",
                "id": 0,
                "reason": f"http_{exc.response.status_code}",
            }
        except Exception as exc:
            logger.error("Notify send error: %s", str(exc))
            return {"status": "failed", "id": 0, "reason": "network"}
        if not isinstance(body, dict):
            return {"status": "sent", "id": 0, "reason": "malformed_body"}
        if "status" not in body:
            body["status"] = "suppressed" if body.get("suppressed") else "sent"
        return body

    async def send_many(
        self,
        items: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for item in items:
            try:
                result: dict[str, object] = await self.send(**item)
            except TypeError:
                # 未知键 = 新版调用方载荷落到旧版客户端（教训#488 why_you 案）：
                # 触达数据键不该炸掉发送，降级并入 data 重试一次。
                known = set(inspect.signature(self.send).parameters)
                unknown = {k: v for k, v in item.items() if k not in known}
                if not unknown:
                    logger.error("Batch notify item failed: bad arguments %s", sorted(item))
                    result = {}
                else:
                    demoted: dict[str, object] = {
                        k: v for k, v in item.items() if k in known
                    }
                    data = dict(demoted.get("data") or {})
                    for key, value in unknown.items():
                        data.setdefault(key, str(value)[:200])
                    demoted["data"] = data
                    try:
                        result = await self.send(**demoted)
                        logger.warning(
                            "Batch notify item: unknown keys %s demoted to data",
                            sorted(unknown),
                        )
                    except Exception as exc:
                        logger.error("Batch notify item failed: %s", str(exc))
                        result = {}
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
            resp: httpx.Response = await self._call("POST", "/api/notify/email",
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
    ) -> bool:
        recipients: list[str] = [to] if isinstance(to, str) else list(to)
        sent: bool = False
        failed: list[str] = []
        for recipient in recipients:
            ok: bool = await self.send_email_via_center(
                to=recipient, subject=subject, body=body, html=html
            )
            if ok:
                sent = True
            else:
                failed.append(recipient)
        if failed:
            # 非幂等 POST 不自动重发（S62）：notifyCenter 可能已投出只是响应
            # 超时，原样重试=同一人收两封。失败收件人只留日志与返回值。
            logger.error(
                "send_email failed recipients=%s subject=%s", failed, str(subject)[:50]
            )
        return sent

    async def send_sms(
        self,
        phone: str,
        template_code: str,
        template_param: Optional[dict[str, object]] = None,
        sign_name: str = "",
    ) -> bool:
        try:
            resp: httpx.Response = await self._call("POST", "/api/notify/sms",
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

    async def stats(self, days: int = 14) -> dict[str, object]:
        try:
            resp: httpx.Response = await self._call("GET", "/api/notify/stats",
                params={"days": days},
                headers=await self._headers(),
            )
            resp.raise_for_status()
            body: dict[str, object] = resp.json()
        except Exception as exc:
            # r33：失败必须自显——裸 {"apps":[]} 会让消费方把「请求失败」
            # 误读成「无行」（灵犀 brief_reach no_self_row 假象 11-11 生产实锤，
            # 7 天 9 发在场却恒报无行）。error 键 additive，存量消费方零破坏。
            logger.error("Notify stats error: %s", str(exc))
            return {"apps": [], "stats_error": str(exc)[:200]}
        if not isinstance(body, dict):
            return {"apps": [], "stats_error": "malformed_body"}
        return body

    async def read_status(self, ids: list[int]) -> dict[int, bool]:
        """批量查询通知已读状态（notifyCenter ≥2026-10 read-status 端点）。

        返回 {id: is_read}；查无此行/请求失败时该 id 缺失——消费方以
        「id 不在结果里=证据链缺失」处理，不得当成已读或未读（教训#373）。
        """
        if not ids:
            return {}
        try:
            resp: httpx.Response = await self._call("GET", "/api/notify/read-status",
                params={"ids": ",".join(str(i) for i in ids[:100])},
                headers=await self._headers(),
            )
            resp.raise_for_status()
            body: dict[str, object] = resp.json()
        except Exception as exc:
            logger.warning("Notify read_status error: %s", str(exc))
            return {}
        result: dict[int, bool] = {}
        for item in body.get("statuses") or []:
            if isinstance(item, dict) and item.get("exists") and item.get("id"):
                result[int(item["id"])] = bool(item.get("is_read"))
        return result

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


async def unread_status(notify_id: int) -> Optional[bool]:
    """回执行号→已读状态三态：True=未读 / False=已读 / None=证据缺失。

    None 语义（教训#373）：旧 nexus 无 read_status 方法、请求失败、行不存在——
    证据链缺失期间不可当已读也不可当未读。收归自 travelMate is_unread /
    challengePlanet _is_unread 跨仓克隆（dup_block_scan 簇4，2026-11-04）。
    """
    if not notify_id:
        return None
    try:
        client = get_notify_client()
        method = getattr(client, "read_status", None)
        if method is None:
            return None
        statuses = await method([int(notify_id)])
    except Exception as exc:
        logger.warning("read_status check failed: %s", exc)
        return None
    if int(notify_id) not in statuses:
        return None
    return not statuses[int(notify_id)]


from nexus.notify_api import (  # noqa: E402
    DEFAULT_TASK_FAILURE_RULES,
    friendly_task_failure,
    notify_task_result,
    send_admin_email,
    send_email,
    send_notification,
    send_sms,
    send_webhook_robot,
    try_send,
)
from nexus.notify_proxy import register_notify_proxy  # noqa: E402

__all__ = [
    "NotifyClient", "async_init_notify_client", "get_notify_client",
    "send_notification", "try_send", "send_email", "send_admin_email", "send_sms", "send_webhook_robot",
    "DEFAULT_TASK_FAILURE_RULES", "friendly_task_failure", "notify_task_result",
    "register_notify_proxy",
]
