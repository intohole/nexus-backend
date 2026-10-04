"""用户级 LLM 用量计量：惰性批量上报 usercenter（usage_meters），计费不扣钱。

从 nexus.credits 拆出的计量域：缓冲区 + 延迟批量上报 + 失败回队；
免费期即开始积累，作为定价校准与未来按量计费的数据底座。
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from typing import Any, Optional

from nexus.context import get_request_id, get_user_id
from nexus.logging import get_logger

from nexus.llm_config import resolve_app_name

logger = get_logger("nexus.credits")

METER_FLUSH_THRESHOLD = 20
METER_FLUSH_DELAY = 15.0
METER_BUFFER_MAX = 500


class MeteringBuffer:
    def __init__(self, report_items, resolve_user=None) -> None:
        self._buffer: deque = deque(maxlen=METER_BUFFER_MAX)
        self._flush_task: Optional[asyncio.Task] = None
        self._seq = 0
        self._report_items = report_items
        self._resolve_user = resolve_user

    def _uid(self, user_id: int | str | None) -> str:
        if user_id:
            return str(user_id)
        if self._resolve_user is not None:
            return str(self._resolve_user() or "")
        return str(get_user_id() or "")

    def append(
        self,
        kind: str,
        feature: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        calls: int = 1,
        duration_ms: int = 0,
        user_id: int | str | None = None,
        app_key: str | None = None,
        request_id: str | None = None,
    ) -> bool:
        """入队一条计量；无用户归因返回 False（匿名调用跳过）。"""
        uid = self._uid(user_id)
        if not uid:
            return False
        self._seq += 1
        rid = request_id or get_request_id() or uuid.uuid4().hex
        self._buffer.append({
            "user_id": int(uid),
            "app": app_key or resolve_app_name(),
            "feature": feature or "",
            "kind": kind or "chat",
            "model": model or "",
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "calls": int(calls or 1),
            "duration_ms": int(duration_ms or 0),
            "meter_key": f"{rid}:{self._seq}"[:100],
        })
        self._schedule_flush()
        return True

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = 0.1 if len(self._buffer) >= METER_FLUSH_THRESHOLD else METER_FLUSH_DELAY
        self._flush_task = loop.create_task(self._flush_after(delay))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        items = list(self._buffer)
        self._buffer.clear()
        try:
            res = await self._report_items(items)
            if res.get("success") is False:
                self._requeue(items)
                logger.warning("计量上报被拒绝，已回队重试: %s", res.get("message"))
        except Exception as exc:
            self._requeue(items)
            logger.warning("计量上报失败，已回队重试: %s", exc)

    def _requeue(self, items: list) -> None:
        for item in reversed(items):
            self._buffer.appendleft(item)


async def report_llm_usage(
    *,
    app_name: str,
    kind: str,
    request_id: str,
    response: Any = None,
    latency_s: float = 0.0,
    calls: int = 1,
    feature: str = "",
    append_meter=None,
) -> None:
    """LLM 调用出口计量钩子（永不抛错，匿名调用自动跳过）。append_meter 由 credits 注入。"""
    try:
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        if not input_tokens and not output_tokens:
            input_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        meter = append_meter or _default_meter
        meter(
            kind=kind,
            feature=feature,
            model=str(getattr(response, "model", "") or ""),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            calls=calls,
            duration_ms=int(latency_s * 1000),
            app_key=app_name,
            request_id=request_id,
        )
    except Exception:
        pass


def _default_meter(**kwargs: Any) -> None:
    from nexus.credits import get_credits_service

    get_credits_service().report_meter(**kwargs)
