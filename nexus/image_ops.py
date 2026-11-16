"""图片预处理：EXIF 纠正、按档位缩放、token 估算与内容指纹。"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import re
from dataclasses import dataclass

TOKEN_PIXELS = 784
MIN_TOKENS = 4
_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.*)$", re.S)


@dataclass(frozen=True)
class ImageMeta:
    width: int
    height: int
    bytes_size: int
    est_tokens: int
    mime: str
    sha256: str
    resized: bool


_EMPTY_META = ImageMeta(0, 0, 0, 0, "", "", False)


def estimate_tokens(width: int, height: int) -> int:
    if width <= 0 or height <= 0:
        return 0
    return max(MIN_TOKENS, math.ceil(width * height / TOKEN_PIXELS))


def _target_size(width: int, height: int, max_side: int, target_tokens: int) -> tuple[int, int]:
    scale = min(1.0, max_side / max(width, height))
    out_w = max(1, round(width * scale))
    out_h = max(1, round(height * scale))
    if target_tokens > 0:
        estimated = estimate_tokens(out_w, out_h)
        if estimated > target_tokens:
            factor = math.sqrt(target_tokens / estimated)
            out_w = max(1, round(out_w * factor))
            out_h = max(1, round(out_h * factor))
    return out_w, out_h


def prepare_bytes(
    data: bytes,
    *,
    max_side: int = 2048,
    target_tokens: int = 0,
    quality: int = 88,
    lossless: bool = False,
) -> tuple[bytes, str, ImageMeta]:
    digest = hashlib.sha256(data).hexdigest()[:16]
    try:
        from PIL import Image, ImageOps
    except Exception:
        return data, "", ImageMeta(0, 0, len(data), 0, "", digest, False)
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            out_w, out_h = _target_size(width, height, max_side, target_tokens)
            resized = (out_w, out_h) != (width, height)
            if resized:
                image = image.resize((out_w, out_h), Image.LANCZOS)
            if lossless:
                if image.mode not in ("RGB", "RGBA", "L"):
                    image = image.convert("RGBA")
                fmt, mime = "PNG", "image/png"
            else:
                if image.mode != "RGB":
                    image = image.convert("RGB")
                fmt, mime = "JPEG", "image/jpeg"
            output = io.BytesIO()
            if fmt == "JPEG":
                image.save(output, format=fmt, quality=quality, optimize=True)
            else:
                image.save(output, format=fmt, optimize=True)
            payload = output.getvalue()
            return payload, mime, ImageMeta(
                out_w, out_h, len(payload), estimate_tokens(out_w, out_h), mime, digest, resized
            )
    except Exception:
        return data, "", ImageMeta(0, 0, len(data), 0, "", digest, False)


def prepare_data_url(
    url: str,
    *,
    max_side: int = 2048,
    target_tokens: int = 0,
    quality: int = 88,
) -> tuple[str, ImageMeta]:
    if not isinstance(url, str):
        return url, _EMPTY_META
    match = _DATA_URL_RE.match(url)
    if not match:
        return url, _EMPTY_META
    mime, encoded = match.group(1), match.group(2)
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        return url, _EMPTY_META
    lossless = mime in ("image/png", "image/webp")
    payload, out_mime, meta = prepare_bytes(
        raw,
        max_side=max_side,
        target_tokens=target_tokens,
        quality=quality,
        lossless=lossless,
    )
    if meta.width <= 0:
        return url, meta
    rewritten = f"data:{out_mime};base64,{base64.b64encode(payload).decode('ascii')}"
    return rewritten, meta


__all__ = [
    "ImageMeta",
    "TOKEN_PIXELS",
    "estimate_tokens",
    "prepare_bytes",
    "prepare_data_url",
]
