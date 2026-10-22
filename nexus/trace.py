"""跨服务调用链透传：出站统一注入 X-Request-ID，使 req_id 从进程内日志装饰升格为跨服务串联合同。

语义：
- ensure_request_id(): 取当前请求上下文 id；缺席则生成并回填 contextvar
  （使出站头与后续本地日志行同 id——后台 job 无入站中间件时自动建链）；
- outbound_headers(): 出站请求头装配单源，全部出站底座统一消费；
- 零漂移：无请求语境（contextvar 空）时 ensure 生成新 id——header 恒在场，
  但对不识别该头的下游零影响（多余 header 语义无害）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from nexus.context import get_request_id, set_request_context

TRACE_HEADER: str = "X-Request-ID"


def ensure_request_id() -> str:
    rid: str = get_request_id()
    if not rid:
        rid = uuid.uuid4().hex[:16]
        set_request_context(request_id=rid)
    return rid


def outbound_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers: dict[str, str] = {TRACE_HEADER: ensure_request_id()}
    if extra:
        headers.update(extra)
    return headers
