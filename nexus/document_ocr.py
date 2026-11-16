"""文档 OCR 分流：先转写文字，再用文本 LLM 结构化，降低图像 token 与重复上传。"""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Union

from nexus.llm_client import LLMJsonClient
from nexus.logging import get_logger
from nexus.untrusted import wrap_untrusted
from nexus.vision import VisionService, get_vision_service

logger = get_logger("nexus.document_ocr")

OCR_PROMPT = (
    "逐字转写图片中的全部文字，保持原有顺序与换行。"
    "数字、符号、单位、拼写按原样输出；看不清的字符写 [?]，禁止猜测、禁止补全、禁止解释。"
    "只输出转写文本。"
)
OCR_SYSTEM = "你是文档转写引擎，只做逐字转写，不做总结、不做推断。"
MIN_TEXT_CHARS = 40

_DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.*)$", re.S)


@dataclass(frozen=True)
class OcrResult:
    text: str
    engine: str
    confidence: float = 0.0


class OcrEngine(Protocol):
    name: str

    async def extract(self, images: list[Union[str, bytes, bytearray]]) -> OcrResult: ...


def _decode_image(image: Union[str, bytes, bytearray]) -> Optional[bytes]:
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if not isinstance(image, str):
        return None
    match = _DATA_URL_RE.match(image)
    if not match:
        return None
    try:
        return base64.b64decode(match.group(1))
    except Exception:
        return None


class TesseractEngine:
    name = "tesseract"

    @staticmethod
    def available() -> bool:
        try:
            import pytesseract  # noqa: F401
            from PIL import Image  # noqa: F401
            return True
        except Exception:
            return False

    async def extract(self, images: list[Union[str, bytes, bytearray]]) -> OcrResult:
        def _run() -> OcrResult:
            import io

            import pytesseract
            from PIL import Image

            parts: list[str] = []
            for image in images:
                raw = _decode_image(image)
                if not raw:
                    continue
                with Image.open(io.BytesIO(raw)) as img:
                    parts.append(pytesseract.image_to_string(img, lang="chi_sim+eng"))
            return OcrResult(text="\n".join(part.strip() for part in parts if part.strip()),
                             engine=self.name)

        return await asyncio.to_thread(_run)


class VlmOcrEngine:
    name = "vlm"

    def __init__(self, vision: Optional[VisionService] = None) -> None:
        self._vision = vision or get_vision_service()

    async def extract(self, images: list[Union[str, bytes, bytearray]]) -> OcrResult:
        text = await self._vision.recognize(
            OCR_PROMPT,
            images,
            system=OCR_SYSTEM,
            temperature=0.0,
            profile="ocr",
            feature="ocr:vlm",
        )
        return OcrResult(text=text.strip(), engine=self.name)


def get_engine(name: str = "auto", vision: Optional[VisionService] = None) -> OcrEngine:
    key = (name or "auto").strip().lower()
    if key in ("auto", "tesseract") and TesseractEngine.available():
        return TesseractEngine()
    return VlmOcrEngine(vision)


class DocumentReader:
    def __init__(
        self,
        ocr: Optional[OcrEngine] = None,
        vision: Optional[VisionService] = None,
        text_client: Optional[LLMJsonClient] = None,
    ) -> None:
        self._vision = vision or get_vision_service()
        self._ocr = ocr or get_engine("auto", self._vision)
        self._text_client = text_client

    async def extract_text(
        self,
        images: Iterable[Union[str, bytes, bytearray]],
        *,
        engine: str = "auto",
    ) -> OcrResult:
        items = list(images)
        runner = self._ocr if engine in ("", "auto") else get_engine(engine, self._vision)
        return await runner.extract(items)

    async def extract_json(
        self,
        prompt: str,
        images: Iterable[Union[str, bytes, bytearray]],
        *,
        system: str = "",
        task_type: str = "extraction",
        temperature: float = 0.2,
        max_tokens: int = 1500,
        model: Optional[str] = None,
        engine: str = "auto",
        min_text_chars: int = MIN_TEXT_CHARS,
        no_cache: bool = False,
    ) -> dict[str, object]:
        items = list(images)
        try:
            result = await self.extract_text(items, engine=engine)
            text = result.text.strip()
        except Exception as exc:
            logger.warning("ocr extract failed: %s", exc)
            text = ""
        if len(text) < min_text_chars:
            return await self._vision.recognize_json(
                prompt, items, system=system, temperature=temperature,
                feature="ocr:fallback", profile="ocr", no_cache=no_cache,
            )
        guarded = wrap_untrusted(text, source=f"ocr:{result.engine}")
        text_prompt = f"{prompt}\n\n[OCR 转写文本]\n{guarded}"
        client = self._text_client or LLMJsonClient(
            task_type=task_type, temperature=temperature, max_tokens=max_tokens, model=model,
        )
        try:
            data = await client.ask_json(prompt=text_prompt, system=system)
            if isinstance(data, dict) and data and set(data) != {"raw_response"}:
                return data
        except Exception as exc:
            logger.warning("ocr text structuring failed: %s", exc)
        return await self._vision.recognize_json(
            prompt, items, system=system, temperature=temperature,
            feature="ocr:fallback", profile="ocr", no_cache=no_cache,
        )


_reader: Optional[DocumentReader] = None


def get_document_reader() -> DocumentReader:
    global _reader
    if _reader is None:
        _reader = DocumentReader()
    return _reader


__all__ = [
    "DocumentReader",
    "MIN_TEXT_CHARS",
    "OcrEngine",
    "OcrResult",
    "TesseractEngine",
    "VlmOcrEngine",
    "get_document_reader",
    "get_engine",
]
