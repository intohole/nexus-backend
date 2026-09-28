from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.fastapi_setup import setup_static_files


def _build(tmp_path: Path, with_index: bool = True) -> TestClient:
    if with_index:
        (tmp_path / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (tmp_path / "asset.txt").write_text("asset", encoding="utf-8")
    app: FastAPI = FastAPI()
    setup_static_files(app, directory=str(tmp_path), spa_fallback=True, path_prefix="")
    return TestClient(app)


def test_serves_existing_static_file(tmp_path: Path) -> None:
    resp = _build(tmp_path).get("/asset.txt")
    assert resp.status_code == 200
    assert resp.text == "asset"


def test_deep_link_falls_back_to_index(tmp_path: Path) -> None:
    resp = _build(tmp_path).get("/deep/link")
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_missing_file_returns_json_404_for_api_client(tmp_path: Path) -> None:
    resp = _build(tmp_path, with_index=False).get(
        "/missing", headers={"accept": "application/json"}
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_missing_route_returns_html_404_for_browser(tmp_path: Path) -> None:
    resp = _build(tmp_path, with_index=False).get("/missing", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert "text/html" in resp.headers["content-type"]


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    (tmp_path.parent / "outside.txt").write_text("secret", encoding="utf-8")
    resp = _build(tmp_path).get("/..%2foutside.txt")
    assert resp.status_code == 404
