"""上传存储：大小受限读取与文件落盘工具。"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

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


async def save_upload(
    file: UploadFile,
    *,
    upload_dir: str | Path,
    allowed_ext: Optional[dict[str, str]] = None,
    max_size_mb: float = 10,
    subdir: str = "",
    url_prefix: str = "/media",
) -> dict[str, object]:
    if allowed_ext is not None and (file.content_type or "") not in allowed_ext:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    raw = await read_limited(file, int(max_size_mb * 1024 * 1024))

    dir_path = Path(upload_dir)
    if not dir_path.is_absolute():
        dir_path = Path.cwd() / dir_path
    if subdir:
        dir_path = dir_path / subdir
    dir_path.mkdir(parents=True, exist_ok=True)

    ext = (allowed_ext or {}).get(file.content_type or "", "")
    name = f"{uuid.uuid4().hex}{ext}"
    target = dir_path / name
    target.write_bytes(raw)

    url = f"{url_prefix}/{subdir}/{name}" if subdir else f"{url_prefix}/{name}"
    return {
        "url": url,
        "name": name,
        "path": str(target),
        "size": len(raw),
        "content_type": file.content_type or "",
    }
