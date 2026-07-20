from __future__ import annotations

import functools
from typing import Awaitable, Callable, TypeVar

from fastapi import HTTPException

from nexus.context import get_request_id
from nexus.logging import get_logger
from nexus.uc_sdk_helper import standard_err

T = TypeVar("T")

logger = get_logger("nexus.api_decorators")


def handle_api_errors(operation_name: str = "operation") -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """API错误处理装饰器，统一处理异常并返回标准错误响应"""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as e:
                request_id: str = get_request_id() or "-"
                logger.warning(
                    "API ValueError in '%s' [req_id=%s]: %s",
                    operation_name,
                    request_id,
                    str(e),
                )
                standard_err(message=str(e), status_code=400)
            except Exception as e:
                request_id = get_request_id() or "-"
                logger.error(
                    "API error in '%s' [req_id=%s]: %s",
                    operation_name,
                    request_id,
                    str(e),
                    exc_info=True,
                )
                standard_err(message=f"{operation_name}失败: {e}", status_code=500)

        return wrapper

    return decorator
