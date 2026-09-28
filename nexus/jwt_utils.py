"""JWT 签发与验签工具（python-jose 单一实现，与 middleware_auth/uc_sdk 同栈）。"""
from __future__ import annotations

from typing import Any, Optional

from jose import JWTError, jwt

DEFAULT_ALGORITHM = "HS256"


def sign_jwt(
    payload: dict[str, Any],
    secret: str,
    algorithm: str = DEFAULT_ALGORITHM,
) -> str:
    """签发 JWT，secret 由调用方（业务密钥）提供。"""
    return jwt.encode(payload, secret, algorithm=algorithm)


def verify_jwt(
    token: str,
    secret: str,
    algorithm: str = DEFAULT_ALGORITHM,
) -> Optional[dict[str, Any]]:
    """验证 JWT，返回 payload；过期/非法返回 None。"""
    try:
        result = jwt.decode(token, secret, algorithms=[algorithm])
    except JWTError:
        return None
    return result if isinstance(result, dict) else None
