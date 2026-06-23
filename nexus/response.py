from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(default=200)
    message: str = Field(default="success")
    data: Optional[T] = None
    trace_id: Optional[str] = None


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class PaginatedResponse(BaseModel, Generic[T]):
    code: int = Field(default=200)
    message: str = Field(default="success")
    data: list[T] = Field(default_factory=list)
    pagination: PaginationMeta
    trace_id: Optional[str] = None


def success_response(
    data: object = None,
    message: str = "success",
    trace_id: Optional[str] = None,
) -> dict[str, object]:
    result: dict[str, object] = {"code": 200, "message": message}
    if data is not None:
        result["data"] = data
    if trace_id:
        result["trace_id"] = trace_id
    return result


def error_response(
    message: str,
    code: int = 500,
    error_code: Optional[str] = None,
    details: Optional[dict[str, object]] = None,
    trace_id: Optional[str] = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "code": code,
        "message": message,
    }
    if error_code:
        result["error_code"] = error_code
    if details:
        result["details"] = details
    if trace_id:
        result["trace_id"] = trace_id
    return result


def paginate_response(
    data: list[object],
    total: int,
    page: int,
    page_size: int,
    trace_id: Optional[str] = None,
) -> dict[str, object]:
    total_pages: int = (total + page_size - 1) // page_size if page_size > 0 else 0
    result: dict[str, object] = {
        "code": 200,
        "message": "success",
        "data": data,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }
    if trace_id:
        result["trace_id"] = trace_id
    return result
