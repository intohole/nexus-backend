"""请求上下文：request_id/用户/组织的 contextvars 存取。"""
from __future__ import annotations

import contextvars
from typing import Optional

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_request_id", default=""
)
_user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_user_id", default=""
)
_org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_org_id", default=""
)


def set_request_context(
    request_id: Optional[str] = None,
    user_id: Optional[str] = None,
    org_id: Optional[str] = None,
) -> None:
    if request_id is not None:
        _request_id_var.set(request_id)
    if user_id is not None:
        _user_id_var.set(user_id)
    if org_id is not None:
        _org_id_var.set(org_id)


def get_request_id() -> str:
    return _request_id_var.get()


def get_user_id() -> str:
    return _user_id_var.get()

