from __future__ import annotations

from typing import Any, Optional

import jwt

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
    except jwt.PyJWTError:
        return None
    return result
