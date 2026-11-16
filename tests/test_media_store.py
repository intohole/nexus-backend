from __future__ import annotations

import base64

from nexus.media_store import get_data_url, offload_data_urls, put_data_url

_DATA_URL = "data:image/png;base64," + base64.b64encode(b"fake-image-bytes").decode("ascii")


def test_put_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_MEDIA_DIR", str(tmp_path))
    handle = put_data_url(_DATA_URL)
    assert handle and handle.startswith("img_")
    assert get_data_url(handle) == _DATA_URL


def test_offload_replaces_data_url_with_handle(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_MEDIA_DIR", str(tmp_path))
    text, handles = offload_data_urls(f"结果: {_DATA_URL} 完成")
    assert text == f"结果: [image_ref: {handles[0]}] 完成"
    assert get_data_url(handles[0]) == _DATA_URL


def test_invalid_handle_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_MEDIA_DIR", str(tmp_path))
    assert get_data_url("../../etc/passwd") is None
    assert get_data_url("img_ffffffffffffffff") is None
    assert put_data_url("https://a.com/x.png") is None
