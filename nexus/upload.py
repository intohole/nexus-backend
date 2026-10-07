"""上传校验原语：扩展名白名单 + 大小钳制 + 流式限量读取。

HTTPException 直抛与 auth_anon 等 web 原语同惯例；业务侧需要领域异常时
捕获后转译（见 resumeAI FileParseError）。
"""
from __future__ import annotations

import os

from fastapi import HTTPException, UploadFile, status

_CHUNK_SIZE: int = 1024 * 1024


def split_ext(filename: str | None) -> str:
    """小写扩展名（含点）；无扩展名返回空串。"""
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lower()


def _over_limit_message(max_bytes: int) -> str:
    return f"文件超过{max_bytes // (1024 * 1024)}MB限制"


def validate_upload(
    filename: str | None,
    size: int,
    *,
    allowed_exts: set[str] | frozenset[str] | tuple[str, ...],
    max_bytes: int,
) -> str:
    """校验扩展名白名单与大小，返回小写扩展名；违规抛 400/413。"""
    ext = split_ext(filename)
    if ext not in allowed_exts:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不支持的文件类型: {ext or '(无扩展名)'}",
        )
    if size > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            _over_limit_message(max_bytes),
        )
    return ext


async def read_upload_with_limit(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取上传内容，累计超限立即抛 413（不整读大文件）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                _over_limit_message(max_bytes),
            )
        chunks.append(chunk)
    return b"".join(chunks)
