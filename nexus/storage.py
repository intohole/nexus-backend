"""上传存储：大小受限读取工具。"""
from __future__ import annotations

from fastapi import HTTPException, UploadFile


async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail="文件超过大小限制")
        chunks.append(chunk)
    if not chunks:
        raise HTTPException(status_code=400, detail="文件为空")
    return b"".join(chunks)

