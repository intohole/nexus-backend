from __future__ import annotations

from typing import Optional

DEFAULT_DISPLAY_NAME = "星友"
_AUTO_NAME_PREFIXES = ("user_", "uc_", "guest_", "用户")
_PLACEHOLDER_TOKENS = {"unknown", "未知", "undefined", "null", "none", "n/a", "未设置", "未命名", "匿名"}
_SYNTHETIC_EMAIL_MARK = "@users.internal"


def _is_blank(value: Optional[str]) -> bool:
    return not value or not str(value).strip()


def _is_synthetic_email(email: Optional[str]) -> bool:
    return bool(email and _SYNTHETIC_EMAIL_MARK in str(email).strip().lower())


def mask_phone(phone: Optional[str]) -> str:
    p = str(phone or "")
    if len(p) >= 7:
        return f"{p[:3]}****{p[-4:]}"
    return "****" if p else ""


def mask_email(email: Optional[str]) -> str:
    e = str(email or "")
    if "@" not in e:
        return e
    local, _, domain = e.partition("@")
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def _looks_auto_generated(name: str, user_id: str = "") -> bool:
    value = str(name or "").strip()
    if not value:
        return True
    if value.isdigit():
        return True
    if value.lower() in _PLACEHOLDER_TOKENS:
        return True
    if user_id and value == str(user_id):
        return True
    if value.lower().startswith(_AUTO_NAME_PREFIXES):
        return True
    return False


def resolve_display_name(user: dict) -> str:
    user_id = str(user.get("user_id") or "")
    for key in ("display_name", "nickname", "username", "full_name"):
        raw = user.get(key)
        if raw is None:
            continue
        value = str(raw).strip()
        if value and not _looks_auto_generated(value, user_id):
            return value
    email = str(user.get("email") or "").strip()
    if email and not _is_synthetic_email(email):
        return mask_email(email)
    phone = str(user.get("phone") or "").strip()
    if phone:
        return mask_phone(phone)
    return DEFAULT_DISPLAY_NAME