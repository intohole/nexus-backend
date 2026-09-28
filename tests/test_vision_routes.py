from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PNG = b"\x89PNG\r\n\x1a\nfakepng"
_JPG = b"\xff\xd8\xff\xe0fakejpeg"
_TXT = b"not an image"


@pytest.fixture
def client(monkeypatch):
    from nexus.auth import get_current_user_id_required
    from nexus.vision import VisionService
    from nexus.vision_routes import create_vision_router

    service = VisionService()
    service._model = "glm-4.6v-flash"

    async def fake_recognize(prompt, images, system=None, temperature=0.2):
        assert prompt == "图中有什么"
        assert images and images[0].startswith(b"\x89PNG")
        return "一只猫"

    async def fake_recognize_json(prompt, images, system=None, temperature=0.2):
        return {"items": ["猫"]}

    monkeypatch.setattr(service, "recognize", fake_recognize)
    monkeypatch.setattr(service, "recognize_json", fake_recognize_json)
    monkeypatch.setattr("nexus.vision_routes.get_vision_service", lambda: service)

    app = FastAPI()
    app.include_router(create_vision_router())
    app.dependency_overrides[get_current_user_id_required] = lambda: "user-1"
    return TestClient(app, raise_server_exceptions=True)


def _auth(client):
    return {"Authorization": "Bearer x"}


def test_recognize_text(client):
    resp = client.post(
        "/api/vision/recognize",
        files=[("files", ("a.png", _PNG, "image/png"))],
        data={"prompt": "图中有什么"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["data"]["content"] == "一只猫"
    assert body["data"]["model"] == "glm-4.6v-flash"


def test_recognize_json_mode(client):
    resp = client.post(
        "/api/vision/recognize",
        files=[("files", ("a.png", _PNG, "image/png"))],
        data={"prompt": "图中有什么", "json_mode": "true"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["result"] == {"items": ["猫"]}


def test_recognize_multi_images(client, monkeypatch):
    from nexus.vision import VisionService

    service = VisionService()
    service._model = "glm-4.6v-flash"
    seen: dict = {}

    async def fake_recognize(prompt, images, system=None, temperature=0.2):
        seen["count"] = len(images)
        return "两张图"

    monkeypatch.setattr(service, "recognize", fake_recognize)
    monkeypatch.setattr("nexus.vision_routes.get_vision_service", lambda: service)

    resp = client.post(
        "/api/vision/recognize",
        files=[
            ("files", ("a.png", _PNG, "image/png")),
            ("files", ("b.jpg", _JPG, "image/jpeg")),
        ],
        data={"prompt": "对比两图"},
    )
    assert resp.status_code == 200
    assert seen["count"] == 2
    assert resp.json()["data"]["content"] == "两张图"


def test_recognize_rejects_bad_type(client):
    resp = client.post(
        "/api/vision/recognize",
        files=[("files", ("a.txt", _TXT, "text/plain"))],
        data={"prompt": "图中有什么"},
    )
    assert resp.status_code == 400


def test_recognize_rejects_empty_prompt(client):
    resp = client.post(
        "/api/vision/recognize",
        files=[("files", ("a.png", _PNG, "image/png"))],
        data={"prompt": "  "},
    )
    assert resp.status_code == 400


def test_recognize_requires_prompt_field(client):
    resp = client.post(
        "/api/vision/recognize",
        files=[("files", ("a.png", _PNG, "image/png"))],
    )
    assert resp.status_code == 400


def test_model_endpoint(client):
    resp = client.get("/api/vision/model")
    assert resp.status_code == 200
    assert resp.json()["data"]["model"] == "glm-4.6v-flash"
