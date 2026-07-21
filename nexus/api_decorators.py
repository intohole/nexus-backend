from __future__ import annotations

import functools
import inspect
import typing
from typing import Awaitable, Callable, Optional, TypeVar

from fastapi import HTTPException

from nexus.context import get_request_id
from nexus.errors import NexusError
from nexus.logging import get_logger
from nexus.uc_sdk_helper import standard_err

T = TypeVar("T")

logger = get_logger("nexus.api_decorators")


def _resolved_signature(func: Callable[..., Awaitable[T]]) -> Optional[inspect.Signature]:
    """在函数原始命名空间解析 PEP 563 字符串注解，返回解析后的签名。

    被装饰的路由函数若使用 `from __future__ import annotations`，FastAPI 默认
    会在本装饰器模块的命名空间解析 ForwardRef（functools.wraps 不复制
    __globals__），导致 Pydantic 模型解析失败：OpenAPI 生成 500、请求体被
    误判为查询参数。提前解析并挂到 wrapper.__signature__ 可根治。
    """
    try:
        # include_extras=True 保留 Annotated 元数据（如 Annotated[AsyncSession, Depends(get_db)]）。
        # 默认 False 会把 Annotated[T, ...] 降级为 T，导致 FastAPI 路由参数丢失 Depends，
        # 进而把 AsyncSession 当成普通 Pydantic 字段报 "Invalid args for response field"。
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception:
        return None
    sig = inspect.signature(func)
    params = [
        param.replace(annotation=hints.get(param.name, param.annotation))
        for param in sig.parameters.values()
    ]
    return sig.replace(
        parameters=params,
        return_annotation=hints.get("return", sig.return_annotation),
    )


def preserve_resolved_signature(
    wrapper: Callable[..., Awaitable[T]], func: Callable[..., Awaitable[T]]
) -> Callable[..., Awaitable[T]]:
    """将 func 的已解析签名挂到 wrapper，供各类路由装饰器复用。

    functools.wraps 只复制元数据不复制 __globals__，FastAPI 会在装饰器模块
    命名空间解析 ForwardRef 导致失败；凡包装 FastAPI 路由的装饰器都应调用
    本函数，而不是各自重复实现签名修复逻辑。
    """
    resolved_sig = _resolved_signature(func)
    if resolved_sig is not None:
        wrapper.__signature__ = resolved_sig  # type: ignore[attr-defined]
    return wrapper


def handle_api_errors(operation_name: str = "operation") -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """API错误处理装饰器，统一处理异常并返回标准错误响应"""

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> T:
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                raise
            except NexusError as e:
                request_id: str = get_request_id() or "-"
                logger.warning(
                    "API %s in '%s' [req_id=%s]: %s",
                    e.error_code,
                    operation_name,
                    request_id,
                    e.message,
                )
                standard_err(message=e.message, status_code=e.status_code)
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

        return preserve_resolved_signature(wrapper, func)

    return decorator
