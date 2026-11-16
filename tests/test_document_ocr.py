from __future__ import annotations

import pytest

from nexus.document_ocr import DocumentReader, OcrResult


class FakeOcr:
    name = "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    async def extract(self, images: list[object]) -> OcrResult:
        return OcrResult(text=self.text, engine=self.name)


class FakeVision:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def recognize_json(self, prompt, images, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return {"fallback": True}


class FakeText:
    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        self.result = result or {"key_points": ["a"]}
        self.error = error
        self.prompts: list[str] = []

    async def ask_json(self, prompt: str, system: str, **kwargs):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_ocr_text_path_structures_with_text_llm():
    vision = FakeVision()
    text = FakeText({"key_points": ["营收增长"]})
    reader = DocumentReader(ocr=FakeOcr("营业收入 1000 万元，同比增长 20%。" * 3),
                            vision=vision, text_client=text)
    data = await reader.extract_json("提取要点", ["data:image/png;base64,AAAA"], system="sys")
    assert data == {"key_points": ["营收增长"]}
    assert vision.calls == []
    assert "[OCR 转写文本]" in text.prompts[0]


@pytest.mark.asyncio
async def test_short_ocr_text_falls_back_to_vision():
    vision = FakeVision()
    text = FakeText()
    reader = DocumentReader(ocr=FakeOcr("短"), vision=vision, text_client=text)
    data = await reader.extract_json("提取要点", ["data:image/png;base64,AAAA"], system="sys")
    assert data == {"fallback": True}
    assert len(vision.calls) == 1
    assert vision.calls[0]["kwargs"]["profile"] == "ocr"
    assert text.prompts == []


@pytest.mark.asyncio
async def test_text_llm_error_falls_back_to_vision():
    vision = FakeVision()
    reader = DocumentReader(ocr=FakeOcr("这是一段足够长的 OCR 转写文本。" * 5),
                            vision=vision, text_client=FakeText(error=RuntimeError("boom")))
    data = await reader.extract_json("提取要点", ["data:image/png;base64,AAAA"], system="sys")
    assert data == {"fallback": True}
    assert len(vision.calls) == 1


@pytest.mark.asyncio
async def test_ocr_engine_error_falls_back_to_vision():
    class BrokenOcr:
        name = "broken"

        async def extract(self, images: list[object]) -> OcrResult:
            raise RuntimeError("ocr down")

    vision = FakeVision()
    reader = DocumentReader(ocr=BrokenOcr(), vision=vision, text_client=FakeText())
    data = await reader.extract_json("提取要点", ["data:image/png;base64,AAAA"])
    assert data == {"fallback": True}
    assert len(vision.calls) == 1
