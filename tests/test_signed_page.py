"""nexus.signed_page 原语守底（1.127.0）：签名/校验/落盘/路径/URL 机制层。"""
from __future__ import annotations

import os

import pytest

from nexus.signed_page import (
    archive_page,
    page_path,
    page_secret,
    page_sig,
    page_url,
    valid_slug,
    verify_page_sig,
)


def test_sig_deterministic_and_namespaced(tmp_path):
    a1 = page_sig("s3cret", "brief", "2026-11-08")
    a2 = page_sig("s3cret", "brief", "2026-11-08")
    assert a1 == a2
    assert a1 != page_sig("s3cret", "brief", "2026-11-09")
    assert a1 != page_sig("other", "brief", "2026-11-08")
    assert a1 != page_sig("s3cret", "pulse", "2026-11-08")
    assert len(a1) == 16


def test_verify_rejects_bad_or_missing_sig():
    assert verify_page_sig("s", "brief", "d1", page_sig("s", "brief", "d1"))
    assert not verify_page_sig("s", "brief", "d1", "")
    assert not verify_page_sig("s", "brief", "d1", None)
    assert not verify_page_sig("s", "brief", "d1", "0" * 16)


def test_valid_slug_blocks_traversal_and_business_pattern():
    assert valid_slug("2026-W45")
    assert not valid_slug("../etc")
    assert not valid_slug("")
    assert not valid_slug("a" * 65)
    date_re = __import__("re").compile(r"^\d{4}-W\d{2}$")
    assert valid_slug("2026-W45", date_re)
    assert not valid_slug("evil", date_re)


def test_secret_env_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / ".sec"
    f.write_text("from-file")
    monkeypatch.setenv("TEST_SP_SEC", "from-env")
    assert page_secret("TEST_SP_SEC", str(f)) == "from-env"
    monkeypatch.delenv("TEST_SP_SEC")
    assert page_secret("TEST_SP_SEC", str(f)) == "from-file"
    missing = str(tmp_path / "sub" / ".new")
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    assert page_secret("TEST_SP_SEC", missing, fallback="fb") == "fb"


def test_secret_first_use_writes_0600_file_from_service_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_SP_SEC", raising=False)
    monkeypatch.setenv("SERVICE_TOKEN", "svc-tok")
    target = str(tmp_path / ".gen")
    assert page_secret("TEST_SP_SEC", target) == "svc-tok"
    assert os.path.exists(target)
    mode = os.stat(target).st_mode & 0o777
    assert mode == 0o600
    # 次读走文件（SERVICE_TOKEN 已撤也不变——档案语义永续）
    monkeypatch.delenv("SERVICE_TOKEN")
    assert page_secret("TEST_SP_SEC", target) == "svc-tok"


def test_archive_atomic_and_honest_none(tmp_path):
    assert archive_page(str(tmp_path), "w1", "<html>ok</html>") is not None
    assert page_path(str(tmp_path), "w1") is not None
    assert page_path(str(tmp_path), "w2") is None
    assert archive_page(str(tmp_path), "w3", "   ") is None
    assert archive_page("/proc/definitely-not-writable", "w4", "<html>") is None


def test_page_url_single_source():
    assert page_url("https://x.com/app/pulse", "2026-W45", "sig1") == (
        "https://x.com/app/pulse/2026-W45?sig=sig1"
    )
    assert page_url("https://x.com/app/pulse/", "2026-W45", "sig1") == (
        "https://x.com/app/pulse/2026-W45?sig=sig1"
    )
