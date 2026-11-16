"""跨进程图片句柄：把 data URL 落盘为短句柄，供工具结果卸载与 image_fetch 取回。"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Optional

_DEFAULT_DIR = os.environ.get("NEXUS_MEDIA_DIR", "data/media")
_DEFAULT_TTL = int(os.environ.get("NEXUS_MEDIA_TTL", "900"))
_HANDLE_RE = re.compile(r"^img_[0-9a-f]{16}$")
_DATA_URL_RE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)")


def _media_dir() -> Path:
    path = Path(os.environ.get("NEXUS_MEDIA_DIR", _DEFAULT_DIR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup(directory: Path, ttl: int) -> None:
    cutoff = time.time() - max(ttl, 1)
    try:
        for item in directory.iterdir():
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def put_data_url(data_url: str, ttl: int = _DEFAULT_TTL) -> Optional[str]:
    match = _DATA_URL_RE.fullmatch(str(data_url or "").strip())
    if not match:
        return None
    mime, encoded = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return None
    handle = "img_" + hashlib.sha256(raw).hexdigest()[:16]
    directory = _media_dir()
    _cleanup(directory, ttl)
    binary = directory / f"{handle}.bin"
    mime_file = directory / f"{handle}.mime"
    if not binary.exists():
        temp = binary.with_suffix(".tmp")
        temp.write_bytes(raw)
        os.replace(temp, binary)
        mime_file.write_text(mime, encoding="ascii")
        try:
            os.chmod(binary, 0o600)
            os.chmod(mime_file, 0o600)
        except OSError:
            pass
    else:
        now = time.time()
        os.utime(binary, (now, now))
        os.utime(mime_file, (now, now))
    return handle


def get_data_url(handle: str) -> Optional[str]:
    key = str(handle or "").strip()
    if not _HANDLE_RE.match(key):
        return None
    directory = _media_dir()
    binary = directory / f"{key}.bin"
    mime_file = directory / f"{key}.mime"
    if not binary.exists() or not mime_file.exists():
        return None
    try:
        mime = mime_file.read_text(encoding="ascii").strip() or "image/png"
        raw = binary.read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def offload_data_urls(text: str, ttl: int = _DEFAULT_TTL) -> tuple[str, list[str]]:
    handles: list[str] = []

    def _replace(match: re.Match) -> str:
        handle = put_data_url(match.group(0), ttl=ttl)
        if not handle:
            return match.group(0)
        handles.append(handle)
        return f"[image_ref: {handle}]"

    rewritten = _DATA_URL_RE.sub(_replace, str(text or ""))
    return rewritten, handles


__all__ = ["get_data_url", "offload_data_urls", "put_data_url"]
