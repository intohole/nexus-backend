"""spa_index_response 前缀注入的回归测试（1.26.0 补齐测试洞）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from starlette.datastructures import Headers

from nexus.response import spa_index_response

INDEX_HTML = "<html><head><title>t</title></head><body>hello</body></html>"


def _request(forwarded_prefix: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_prefix is not None:
        headers.append((b"x-forwarded-prefix", forwarded_prefix.encode()))
    return Request(scope={"type": "http", "headers": headers})


def _write_index(tmp_path: Path) -> str:
    index = tmp_path / "index.html"
    index.write_text(INDEX_HTML, encoding="utf-8")
    return str(index)


def test_injects_forwarded_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("PATH_PREFIX", raising=False)
    resp = spa_index_response(_request("/adsmart"), _write_index(tmp_path))
    body = resp.body.decode()
    assert '<script>window.PATH_PREFIX="/adsmart"</script></head>' in body


def test_falls_back_to_env_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH_PREFIX", "/golden")
    resp = spa_index_response(_request(None), _write_index(tmp_path))
    assert 'window.PATH_PREFIX="/golden"' in resp.body.decode()


def test_header_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH_PREFIX", "/from-env")
    resp = spa_index_response(_request("/from-header"), _write_index(tmp_path))
    assert 'window.PATH_PREFIX="/from-header"' in resp.body.decode()
    assert "from-env" not in resp.body.decode()


def test_dangerous_prefix_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("PATH_PREFIX", raising=False)
    for evil in ['"><script>alert(1)</script>', "/a b", "..", "/x;y"]:
        resp = spa_index_response(_request(evil), _write_index(tmp_path))
        assert "window.PATH_PREFIX" not in resp.body.decode()


def test_prefix_html_escaped(tmp_path, monkeypatch):
    resp = spa_index_response(_request('/a"onload="x'), _write_index(tmp_path))
    assert "window.PATH_PREFIX" not in resp.body.decode()


def test_no_prefix_no_injection(tmp_path, monkeypatch):
    monkeypatch.delenv("PATH_PREFIX", raising=False)
    resp = spa_index_response(_request(None), _write_index(tmp_path))
    assert resp.body.decode() == INDEX_HTML


def test_only_first_head_injected(tmp_path):
    index = tmp_path / "index.html"
    index.write_text(INDEX_HTML + "</head>", encoding="utf-8")
    resp = spa_index_response(_request("/pfx"), str(index))
    assert resp.body.decode().count("window.PATH_PREFIX") == 1
