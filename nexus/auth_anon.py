"""匿名标识与登录态合并解析：游客可用的应用（travelMate/nexus-agent 族）单源化。

语义分型由调用方选参，不再各仓手拷分支：
- token 有效 → (user_id, token)
- token 无效 → 401「未授权或登录已过期」
- 无 token   → anon 存在则回落 anon；缺失时 guest_fallback=True 返回 "guest"，
               否则 401「请先登录」
- validate_anon=True 时 anon 必须匹配 ANON_ID_RE
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import Header, HTTPException

from nexus.auth import extract_bearer_token, get_auth_deps
from nexus.logging import get_logger

logger = get_logger("nexus.auth_anon")

ANON_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


async def get_current_user_or_anon(
    authorization: Optional[str] = Header(None),
    anon_id: Optional[str] = Header(None, alias="X-Anon-Id"),
    *,
    guest_fallback: bool = False,
    validate_anon: bool = True,
) -> tuple[str, Optional[str]]:
    token: Optional[str] = extract_bearer_token(authorization)
    anon: str = (anon_id or "").strip()

    if not token:
        if not anon:
            if guest_fallback:
                return ("guest", None)
            raise HTTPException(status_code=401, detail="请先登录")
        if validate_anon and not ANON_ID_RE.fullmatch(anon):
            raise HTTPException(status_code=401, detail="匿名标识不合法")
        return (anon, None)

    try:
        result: Optional[dict[str, object]] = await get_auth_deps().validate_token(token)
    except Exception as exc:
        logger.warning("token validate error: %s", exc)
        result = None
    if result and result.get("user_id"):
        return (str(result["user_id"]), token)
    raise HTTPException(status_code=401, detail="未授权或登录已过期")


async def get_current_user_or_anon_optional(
    authorization: Optional[str] = Header(None),
    anon_id: Optional[str] = Header(None, alias="X-Anon-Id"),
    *,
    guest_fallback: bool = False,
    validate_anon: bool = True,
) -> tuple[str, Optional[str]]:
    """公开元数据端点用：任何 401 情形一律回落 ("guest", None)。"""
    try:
        return await get_current_user_or_anon(
            authorization, anon_id,
            guest_fallback=guest_fallback, validate_anon=validate_anon,
        )
    except HTTPException:
        return ("guest", None)


__all__ = [
    "ANON_ID_RE",
    "get_current_user_or_anon",
    "get_current_user_or_anon_optional",
]
