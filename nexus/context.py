from __future__ import annotations

import contextvars
import uuid
from typing import Optional

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_request_id", default=""
)
_user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_user_id", default=""
)
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_trace_id", default=""
)


def set_request_context(
    request_id: Optional[str] = None,
    user_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> None:
    if request_id is not None:
        _request_id_var.set(request_id)
    if user_id is not None:
        _user_id_var.set(user_id)
    if trace_id is not None:
        _trace_id_var.set(trace_id)


def get_request_id() -> str:
    return _request_id_var.get()


def get_user_id() -> str:
    return _user_id_var.get()


def get_trace_id() -> str:
    return _trace_id_var.get()


def new_request_id() -> str:
    rid: str = str(uuid.uuid4())
    _request_id_var.set(rid)
    return rid


def clear_request_context() -> None:
    _request_id_var.set("")
    _user_id_var.set("")
    _trace_id_var.set("")


class RequestContext:
    def __init__(
        self,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        self._request_id: str = request_id or str(uuid.uuid4())
        self._user_id: str = user_id or ""
        self._token_request: Optional[contextvars.Token] = None
        self._token_user: Optional[contextvars.Token] = None

    def __enter__(self) -> "RequestContext":
        self._token_request = _request_id_var.set(self._request_id)
        self._token_user = _user_id_var.set(self._user_id)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token_request is not None:
            _request_id_var.reset(self._token_request)
        if self._token_user is not None:
            _user_id_var.reset(self._token_user)

    @property
    def request_id(self) -> str:
        return self._request_id

    @property
    def user_id(self) -> str:
        return self._user_id
