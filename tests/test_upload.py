"""upload 原语：扩展名白名单 / 大小钳制 / 流式限量读取。"""
import pytest
from fastapi import HTTPException

from nexus.upload import read_upload_with_limit, split_ext, validate_upload


class _FakeUpload:
    def __init__(self, data: bytes, chunk: int = 1024 * 1024):
        self._data = data
        self._chunk = chunk
        self._pos = 0

    async def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = min(self._pos + max(n, self._chunk), len(self._data))
        out = self._data[self._pos:end]
        self._pos = end
        return out


def test_split_ext() -> None:
    assert split_ext("a.PDF") == ".pdf"
    assert split_ext("noext") == ""
    assert split_ext("") == ""
    assert split_ext(None) == ""


def test_validate_upload_ok_returns_ext() -> None:
    ext = validate_upload("简历.PDF", 100, allowed_exts=(".pdf", ".docx"), max_bytes=1024)
    assert ext == ".pdf"


def test_validate_upload_rejects_ext() -> None:
    with pytest.raises(HTTPException) as ei:
        validate_upload("a.exe", 1, allowed_exts=(".pdf",), max_bytes=1024)
    assert ei.value.status_code == 400


def test_validate_upload_rejects_missing_ext() -> None:
    with pytest.raises(HTTPException) as ei:
        validate_upload(None, 1, allowed_exts=(".pdf",), max_bytes=1024)
    assert ei.value.status_code == 400


def test_validate_upload_rejects_oversize() -> None:
    with pytest.raises(HTTPException) as ei:
        validate_upload("a.pdf", 11 * 1024 * 1024, allowed_exts=(".pdf",), max_bytes=10 * 1024 * 1024)
    assert ei.value.status_code == 413
    assert "10MB" in ei.value.detail


@pytest.mark.asyncio
async def test_read_upload_within_limit() -> None:
    data = b"x" * (1024 * 1024 + 7)
    out = await read_upload_with_limit(_FakeUpload(data), 10 * 1024 * 1024)
    assert out == data


@pytest.mark.asyncio
async def test_read_upload_stops_at_limit() -> None:
    with pytest.raises(HTTPException) as ei:
        await read_upload_with_limit(_FakeUpload(b"y" * 300), 200)
    assert ei.value.status_code == 413


@pytest.mark.asyncio
async def test_read_upload_empty() -> None:
    assert await read_upload_with_limit(_FakeUpload(b""), 100) == b""
