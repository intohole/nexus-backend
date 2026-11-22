"""跨服务调用链透传：出站统一注入 X-Request-ID（跨服务串联合同）+ X-App-Id（调用方身份归因）。

语义：
- ensure_request_id(): 取当前请求上下文 id；缺席则生成并回填 contextvar
  （使出站头与后续本地日志行同 id——后台 job 无入站中间件时自动建链）；
- identity_headers(): 进程引导的 app 名 → {"X-App-Id": app}；未引导/异常=空集不表态
  （与 ironman http_provider_mixin 同源同判据，直连网关链据此归因，h132 匿名桶根治）；
- outbound_headers(): 出站请求头装配单源（trace+身份双通道），全部出站底座统一消费；
  extra 显式键恒优先（探针/评测件自报身份走这里）；
- 零漂移：无请求语境（contextvar 空）时 ensure 生成新 id——header 恒在场，
  但对不识别该头的下游零影响（多余 header 语义无害）。
"""
from __future__ import annotations

import uuid
from typing import Optional

from nexus.context import get_request_id, set_request_context

TRACE_HEADER: str = "X-Request-ID"
APP_ID_HEADER: str = "X-App-Id"


def ensure_request_id() -> str:
    rid: str = get_request_id()
    if not rid:
        rid = uuid.uuid4().hex[:16]
        set_request_context(request_id=rid)
    return rid


def identity_headers() -> dict[str, str]:
    try:
        from ironman.bootstrap import current_app_name

        app = current_app_name()
    except Exception:
        app = ""
    return {APP_ID_HEADER: app} if app else {}


def outbound_headers(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    headers: dict[str, str] = {TRACE_HEADER: ensure_request_id()}
    headers.update(identity_headers())
    if extra:
        headers.update(extra)
    return headers
