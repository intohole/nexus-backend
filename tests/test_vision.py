from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

_PNG = b"\x89PNG\r\n\x1a\nfakepng"
_JPEG = b"\xff\xd8\xff\xe0fakejpeg"
_TXT = b"not an image"


def test_sniff_and_data_url():
    from nexus.vision import sniff_image_mime, to_data_url

    assert sniff_image_mime(_PNG) == "image/png"
    assert sniff_image_mime(_JPEG) == "image/jpeg"
    with pytest.raises(ValueError):
        sniff_image_mime(_TXT)
    expected = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    assert to_data_url(_PNG) == expected


def test_normalize_images():
    from nexus.vision import VisionService

    service = VisionService()
    assert service._normalize_images("https://a.com/1.png") == ["https://a.com/1.png"]
    single = service._normalize_images(_PNG)
    assert len(single) == 1 and single[0].startswith("data:image/png;base64,")
    mixed = service._normalize_images(["https://a.com/1.png", _JPEG])
    assert mixed[0] == "https://a.com/1.png" and mixed[1].startswith("data:image/jpeg;base64,")
    with pytest.raises(ValueError):
        service._normalize_images([])
    with pytest.raises(TypeError):
        service._normalize_images([123])


@pytest.mark.asyncio
async def test_recognize_uses_gateway(monkeypatch):
    from nexus.vision import GATEWAY_VISION_PATH, VisionService

    service = VisionService()
    service._base_url = "http://gateway"
    service._api_key = "sk-test"
    service._model = "glm-4.6v-flash"
    service._resolved = True

    captured: dict = {}

    async def fake_post(self, url, json=None, headers=None):
        captured["url"] = url
        captured["payload"] = json

        class Resp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": "图中是一只橘猫"}}]}

        return Resp()

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    answer = await service.recognize("这是什么动物", _PNG)
    assert answer == "图中是一只橘猫"
    assert captured["url"] == "http://gateway" + GATEWAY_VISION_PATH
    assert captured["payload"]["model"] == "glm-4.6v-flash"
    parts = captured["payload"]["messages"][1]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/png")
    assert parts[1] == {"type": "text", "text": "这是什么动物"}


@pytest.mark.asyncio
async def test_recognize_json_parses(monkeypatch):
    from nexus.vision import VisionService

    service = VisionService()
    service._base_url = "http://gateway"
    service._api_key = "sk-test"
    service._model = "glm-4.6v-flash"
    service._resolved = True

    async def fake_chat(system, prompt, urls, temperature):
        assert "JSON" in system
        return '```json\n{"items": ["杯子"], "count": 1}\n```'

    monkeypatch.setattr(service, "_chat_vision", fake_chat)
    result = await service.recognize_json("清点图中物品", [_PNG])
    assert result == {"items": ["杯子"], "count": 1}


@pytest.mark.asyncio
async def test_recognize_json_invalid_raises(monkeypatch):
    from nexus.vision import VisionService

    service = VisionService()
    service._base_url = "http://gateway"
    service._api_key = "sk-test"
    service._model = "glm-4.6v-flash"
    service._resolved = True

    async def fake_chat(system, prompt, urls, temperature):
        return "完全不是 JSON 的回答"

    monkeypatch.setattr(service, "_chat_vision", fake_chat)
    with pytest.raises(RuntimeError):
        await service.recognize_json("清点图中物品", [_PNG])


@pytest.mark.asyncio
async def test_review_backward_compat(monkeypatch):
    from nexus.vision import VisionService

    service = VisionService()
    service._base_url = "http://gateway"
    service._api_key = "sk-test"
    service._model = "glm-4.6v-flash"
    service._resolved = True

    captured: dict = {}

    async def fake_chat(system, prompt, urls, temperature):
        captured["urls"] = urls
        return "通过"

    monkeypatch.setattr(service, "_chat_vision", fake_chat)
    assert await service.review("审查", "是否合规", "https://a.com/1.png") == "通过"
    assert captured["urls"] == ["https://a.com/1.png"]
