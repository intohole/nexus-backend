"""统一响应结构：成功/错误响应、分页包装与 SPA 入口注入。"""
from __future__ import annotations

import os
import re
from html import escape as html_escape
from pathlib import Path
from typing import Optional

from fastapi import Request
from fastapi.responses import HTMLResponse

_PREFIX_PATTERN = re.compile(r"[a-zA-Z0-9_\-/]+")


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


def paginated_payload(
    items: list[object],
    total: int,
    skip: int = 0,
    limit: int = 20,
) -> dict[str, object]:
    """flat 分页信封 {items,total,skip,limit,has_more}，与 paginate_from_skip 同形。

    DB 侧入口配 nexus.repository.paginate_skip 使用；code 包裹型用 paginate_response。
    """
    return {
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit,
        "has_more": skip + limit < total,
    }


def spa_index_response(request: Request, index_path: str) -> HTMLResponse:
    """读取 SPA index.html 并注入 window.PATH_PREFIX（反代子路径部署场景）。

    前缀优先取 X-Forwarded-Prefix 请求头，缺省回退 PATH_PREFIX 环境变量；
    白名单字符校验通过且 html 转义后注入到 </head> 前。
    """
    html = Path(index_path).read_text(encoding="utf-8")
    prefix = (request.headers.get("X-Forwarded-Prefix") or "").strip() or os.environ.get("PATH_PREFIX", "")
    if prefix and not _PREFIX_PATTERN.fullmatch(prefix):
        prefix = ""
    if prefix:
        inject = f'<script>window.PATH_PREFIX="{html_escape(prefix, quote=True)}"</script>'
        html = html.replace("</head>", inject + "</head>", 1)
    return HTMLResponse(content=html)
