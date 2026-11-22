"""通知统一出口门面：站内/邮件等通知发送与 notifyCenter 客户端管理。

r239 拆分：NotifyClient 传输本体迁 nexus.notify_client（传输客户端 vs
门面/便捷层分层），单例状态与访问器留守本模块——测试 patch 面
（nexus.notify._notify_client / get_notify_client）与业务仓消费形态
零震荡（教训 #258：patch 面跟命名空间不跟类搬家）。
"""
from __future__ import annotations

from typing import Optional

from nexus.infra import get_notify_center_url
from nexus.logging import get_logger
from nexus.notify_client import NotifyClient

logger = get_logger("nexus.notify")

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
    is_sent,
    notify_task_result,
    send_admin_email,
    send_email,
    send_notification,
    send_receipt,
    send_sms,
    send_webhook_robot,
    try_send,
)
from nexus.notify_proxy import register_notify_proxy  # noqa: E402

__all__ = [
    "NotifyClient", "async_init_notify_client", "get_notify_client",
    "send_notification", "send_receipt", "try_send", "send_email", "send_admin_email", "send_sms", "send_webhook_robot",
    "DEFAULT_TASK_FAILURE_RULES", "friendly_task_failure", "notify_task_result",
    "register_notify_proxy", "is_sent",
]
