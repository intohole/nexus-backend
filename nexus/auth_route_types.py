from __future__ import annotations

from typing import Awaitable, Callable

from fastapi import HTTPException

UcSdkProvider = Callable[[], object]
OkWrapper = Callable[[object, str], object]
ErrWrapper = Callable[[str, int], object]
PostActionHook = Callable[[dict[str, object]], Awaitable[None]]
MeTransformer = Callable[[dict[str, object], str], Awaitable[dict[str, object]]]

DEFAULT_ENDPOINTS: frozenset[str] = frozenset(
    {"login", "register", "refresh", "me", "logout", "config", "login-page-config"}
)


def default_ok(data: object, message: str = "") -> object:
    return data


def default_err(message: str, status_code: int) -> object:
    raise HTTPException(status_code=status_code, detail=message)


def map_uc_detail(result: dict[str, object], default_msg: str) -> str:
    detail: object = result.get("detail", result.get("message", default_msg))
    if isinstance(detail, dict):
        return str(detail.get("message", detail.get("detail", default_msg)))
    return str(detail)
