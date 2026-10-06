"""匿名标识与登录态合并解析：游客可用的应用（travelMate/nexus-agent 族）单源化。

公开依赖函数的签名只含 Header 参数——语义分型拆成独立函数而非 keyword-only
标志位：带标志的函数被直接 Depends() 时标志会泄漏成 FastAPI query 参数
（?guest_fallback=true 可把 401 篡改成 guest 放行），这是 nexus-agent 实测抓获的洞。

语义矩阵：
- token 有效 → (user_id, token)
- token 无效 → 401「未授权或登录已过期」
- 无 token   → anon 存在则回落 anon；缺失时 strict 变体 401「请先登录」，
               lax 变体返回 "guest"
- anon 格式  → strict 校验 ANON_ID_RE（不匹配 401「匿名标识不合法」）；
               lax 不校验（travelMate 存量客户端兼容语义）
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import Header, HTTPException

from nexus.auth import extract_bearer_token, get_auth_deps
from nexus.logging import get_logger

logger = get_logger("nexus.auth_anon")

ANON_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


async def _resolve_user_or_anon(
    authorization: Optional[str],
    anon_id: Optional[str],
    *,
    guest_fallback: bool,
    validate_anon: bool,
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


async def get_current_user_or_anon(
    authorization: Optional[str] = Header(None),
    anon_id: Optional[str] = Header(None, alias="X-Anon-Id"),
) -> tuple[str, Optional[str]]:
    """strict 语义：anon 缺失 401、anon 格式校验（nexus-agent 语义）。"""
    return await _resolve_user_or_anon(
        authorization, anon_id, guest_fallback=False, validate_anon=True,
    )


async def get_current_user_or_anon_lax(
    authorization: Optional[str] = Header(None),
    anon_id: Optional[str] = Header(None, alias="X-Anon-Id"),
) -> tuple[str, Optional[str]]:
    """lax 语义：anon 可缺（回落 "guest"）、不校验格式（travelMate 语义）。"""
    return await _resolve_user_or_anon(
        authorization, anon_id, guest_fallback=True, validate_anon=False,
    )


async def get_current_user_or_anon_optional(
    authorization: Optional[str] = Header(None),
    anon_id: Optional[str] = Header(None, alias="X-Anon-Id"),
) -> tuple[str, Optional[str]]:
    """公开元数据端点用：任何 401 情形一律回落 ("guest", None)。"""
    try:
        return await get_current_user_or_anon(authorization, anon_id)
    except HTTPException:
        return ("guest", None)


__all__ = [
    "ANON_ID_RE",
    "get_current_user_or_anon",
    "get_current_user_or_anon_lax",
    "get_current_user_or_anon_optional",
]
