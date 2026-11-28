"""多模态视觉识别服务，统一封装 PromptManager 网关视觉能力（GLM-4.6V-Flash/GLM-4.5V），供各应用复用.

通用能力：单图/多图识别（recognize）、结构化 JSON 识别（recognize_json）、
本地图片字节识别（recognize_bytes，base64 data URL 直传，网关无需可访问图片地址）。
"""

from __future__ import annotations

import base64
import logging
import time
from collections import defaultdict
from types import SimpleNamespace
from typing import Iterable, Optional, Sequence, Union

from nexus._gateway_base import _GatewayServiceBase
from nexus.context import get_request_id
from nexus.credits import CreditsInsufficientError, get_credits_service, report_llm_usage
from nexus.image_ops import ImageMeta, prepare_data_url
from nexus.llm_config import resolve_app_name
from nexus.llm_json import parse_llm_json
from nexus.vision_cache import VisionCache, build_key, get_vision_cache
from nexus.vision_profiles import detail_for, plan_for, resolve_profile

logger = logging.getLogger(__name__)

_FALLBACK_CENSUS: dict[str, int] = defaultdict(int)

# r272 刀24：视觉家族前缀单源（_model_chain 守卫+docstring 同源；glm-4.5v 系
# docstring 自述支持面，deepseek-flash* 系用户钦定多模态 r254/r256）
_VISION_FAMILY_PREFIXES = ("glm-4.6v", "glm-4.5v", "deepseek-flash")

DEFAULT_VISION_MODEL = "glm-4.6v-flash"
JSON_HINT = "只输出一个 JSON 对象，不要包含解释、markdown 代码块或其他文字。"

ImageInput = Union[str, bytes, bytearray]

_MIME_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_mime(data: bytes) -> str:
    for magic, mime in _MIME_MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    raise ValueError("无法识别的图片格式（仅支持 png/jpeg/gif/webp/bmp）")


def to_data_url(data: bytes, mime_type: str | None = None) -> str:
    """图片字节 → base64 data URL（OpenAI 兼容网关均可识别）。"""
    mime = mime_type or sniff_image_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


GATEWAY_VISION_PATH = "/chat/completions"


async def _vision_preflight(model: str) -> None:
    try:
        await get_credits_service().gateway_preflight("vision", model=model)
    except CreditsInsufficientError:
        raise
    except Exception as exc:
        logger.warning("vision preflight 跳过（非欠费异常）: %s", exc)


def _usage_shim(data: dict) -> SimpleNamespace:
    usage = data.get("usage") or {}
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        ),
        model=str(data.get("model") or ""),
    )


def _charge_usage_from(data: dict) -> dict | None:
    usage = data.get("usage") or {}
    details = usage.get("prompt_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    if not cached:
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    if input_tokens <= 0 and output_tokens <= 0:
        return None
    from datetime import datetime, timezone

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached,
        "called_at": datetime.now(timezone.utc).isoformat(),
    }


async def _vision_charge(app_name: str, request_id: str, model: str,
                         usage: dict | None = None) -> None:
    try:
        await get_credits_service().auto_charge_llm(
            kind="vision", app_name=app_name, request_id=request_id, model=model, usage=usage
        )
    except Exception as exc:
        # 计费失败显影（旧形态静默=白嫖用量无 census；不抛以免反转主链路语义）
        logger.error("vision auto_charge 失败（用量未计费）: app=%s model=%s %s", app_name, model, exc)


class VisionService(_GatewayServiceBase):
    _instance: Optional["VisionService"] = None

    @property
    def model(self) -> str:
        return self._model or DEFAULT_VISION_MODEL

    def _normalize_images(self, images: Union[ImageInput, Iterable[ImageInput]]) -> list[str]:
        """URL/data URL/bytes → image_url 列表；单张图片可直接传 str/bytes。"""
        if isinstance(images, (str, bytes, bytearray)):
            items: Sequence[ImageInput] = [images]
        else:
            items = list(images)
        if not items:
            raise ValueError("images 不能为空")
        urls: list[str] = []
        for item in items:
            if isinstance(item, (bytes, bytearray)):
                urls.append(to_data_url(bytes(item)))
            elif isinstance(item, str):
                urls.append(item)
            else:
                raise TypeError(f"不支持的图片来源类型: {type(item).__name__}")
        return urls

    def _prepare_images(
        self, image_urls: list[str], profile: str
    ) -> tuple[list[str], list[ImageMeta]]:
        plan = plan_for(profile)
        prepared: list[str] = []
        metas: list[ImageMeta] = []
        for url in image_urls:
            rewritten, meta = prepare_data_url(
                url, max_side=plan.max_side, target_tokens=plan.target_tokens
            )
            prepared.append(rewritten)
            metas.append(meta)
        return prepared, metas

    def _model_chain(self, start: str) -> list[str]:
        # r270 刀7b：非视觉家族守卫（视觉家族前缀与 deepseek-flash* 除外——
        # deepseek-flash 多模态能力系用户钦定形态 r254/r256 在案，不得误伤；其余未知
        # 模型带图请求=要么报错要么"看不到图"垃圾文本，改派视觉链）。
        # r272 刀24（狩猎C P2）：豁免族提模块常量并补 glm-4.5v——模块 docstring
        # 自述支持 GLM-4.5V，旧白名单只有 glm-4.6v 前缀=显式选型被静默改派+降档
        # census 把正常选型记成救援（读数污染）。
        if not start.startswith(_VISION_FAMILY_PREFIXES):
            logger.warning("vision 非视觉模型 %s 改派视觉链 glm-4.6v-flash（非视觉守卫）", start)
            _FALLBACK_CENSUS[f"{start}->glm-4.6v-flash(非视觉守卫)"] += 1
            start = "glm-4.6v-flash"
        chain = [start]
        if start.startswith("glm-4.6v-flash"):
            for candidate in ("glm-4.6v-flashx", "glm-4.6v"):
                if candidate != start:
                    chain.append(candidate)
        elif start.startswith("deepseek-flash"):
            # r256：deepseek-flash 记忆图像链补升档回退（旧形态单元素链=零回退带病）
            for candidate in ("glm-4.6v-flash", "glm-4.6v"):
                if candidate not in chain:
                    chain.append(candidate)
        return chain

    async def _chat_vision(
        self,
        system: str,
        prompt: str,
        image_urls: list[str],
        temperature: float,
        feature: str = "",
        profile: str = "auto",
        model: Optional[str] = None,
        no_cache: bool = False,
    ) -> str:
        await self._resolve_config("vision_model", DEFAULT_VISION_MODEL)
        self._require_config("vision")
        resolved_profile = resolve_profile(profile)
        detail = detail_for(resolved_profile)
        effective_model = model or str(self._model or DEFAULT_VISION_MODEL)
        cache_key = build_key(
            profile=resolved_profile,
            detail=detail,
            model=effective_model,
            system=system,
            prompt=prompt,
            image_keys=[VisionCache.image_key(url) for url in image_urls],
        )
        if not no_cache:
            cached = get_vision_cache().get(cache_key)
            if cached is not None:
                return cached
        await _vision_preflight(effective_model)
        prepared_urls, metas = self._prepare_images(image_urls, resolved_profile)
        # r112 前缀缓存友好（cache-buster 破坏因子⑦多模态字节漂移的对治半边）：稳定文本
        # 指令前置、动态截图后置——system+文本段构成可命中前缀，图后不再有字节
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        content.extend(
            {"type": "image_url", "image_url": {"url": url, "detail": detail}}
            for url in prepared_urls
        )
        payload = {
            "model": effective_model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "vision_meta": {
                "profile": resolved_profile,
                "image_count": len(prepared_urls),
                "image_detail": detail_for(resolved_profile),
                "est_image_tokens": sum(meta.est_tokens for meta in metas),
                "vision_model": effective_model,
            },
        }
        request_id: str = get_request_id() or "-"
        app_name: str = resolve_app_name()
        start: float = time.monotonic()
        data = await self._post(f"{self._base_url}{GATEWAY_VISION_PATH}", payload, op="vision request")
        await report_llm_usage(
            app_name=app_name, kind="vision", request_id=request_id,
            response=_usage_shim(data), latency_s=time.monotonic() - start,
            feature=feature or f"vision:{resolved_profile}",
        )
        await _vision_charge(app_name, request_id, effective_model, usage=_charge_usage_from(data))
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("vision request returned no choices")
        message = choices[0].get("message") or {}
        content_text = str(message.get("content") or "")
        if not content_text.strip():
            raise RuntimeError("vision request returned empty content")
        if not no_cache:
            get_vision_cache().set(cache_key, content_text)
        return content_text

    async def analyze(
        self,
        prompt: str,
        images: Union[ImageInput, Iterable[ImageInput]],
        system: Optional[str] = None,
        temperature: float = 0.2,
        profile: str = "auto",
        feature: str = "",
        no_cache: bool = False,
        model: Optional[str] = None,
    ) -> str:
        return await self.recognize(
            prompt, images, system=system, temperature=temperature,
            profile=profile, feature=feature, no_cache=no_cache, model=model,
        )

    async def recognize(
        self,
        prompt: str,
        images: Union[ImageInput, Iterable[ImageInput]],
        system: Optional[str] = None,
        temperature: float = 0.2,
        feature: str = "",
        profile: str = "auto",
        no_cache: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """通用图片识别：对单张/多张图片提问，返回模型文本回答。

        images 元素支持图片 URL、base64 data URL 或原始字节（bytes 自动转 data URL）。
        r270 刀7b：接 _model_chain 升档回退（旧形态单次 _chat_vision 零回退——只有
        recognize_json 有链，解析失败才升档；文本面失败直接裸抛）。
        """
        image_urls = self._normalize_images(images)
        await self._resolve_config("vision_model", DEFAULT_VISION_MODEL)
        start_model = str(model or self._model or DEFAULT_VISION_MODEL)
        last_error: Exception | None = None
        for candidate in self._model_chain(start_model):
            try:
                if candidate != start_model:
                    logger.warning("vision 升档回退: %s -> %s", start_model, candidate)
                    _FALLBACK_CENSUS[f"{start_model}->{candidate}"] += 1
                return await self._chat_vision(
                    system or "你是图片识别助手，用简体中文准确回答。", prompt, image_urls, temperature,
                    feature=feature, profile=profile, no_cache=no_cache, model=candidate,
                )
            except CreditsInsufficientError:
                # r272 刀25（狩猎C P3）：计费致命错不做升档重试（recognize_json:330
                # 同款豁免，形制对齐）——欠费全链烧穿=多次 preflight+回退 census 把
                # 计费失败记成模型回退（运营误读）。
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("vision recognize 模型 %s 失败: %s", candidate, exc)
        assert last_error is not None
        raise last_error

    async def recognize_json(
        self,
        prompt: str,
        images: Union[ImageInput, Iterable[ImageInput]],
        system: Optional[str] = None,
        temperature: float = 0.2,
        feature: str = "",
        profile: str = "auto",
        no_cache: bool = False,
        model: Optional[str] = None,
    ) -> dict[str, object]:
        """结构化图片识别：要求模型输出 JSON 并解析为 dict；免费/轻量档解析失败时升档重试。"""
        image_urls = self._normalize_images(images)
        merged_system = f"{system or '你是图片识别助手。'}\n{JSON_HINT}"
        await self._resolve_config("vision_model", DEFAULT_VISION_MODEL)
        start_model = str(model or self._model or DEFAULT_VISION_MODEL)
        last_error = ""
        for candidate in self._model_chain(start_model):
            try:
                if candidate != start_model:
                    logger.warning("vision 升档回退: %s -> %s", start_model, candidate)
                    _FALLBACK_CENSUS[f"{start_model}->{candidate}"] += 1
                raw = await self._chat_vision(
                    merged_system, prompt, image_urls, temperature,
                    feature=feature, profile=profile, model=candidate, no_cache=no_cache,
                )
                result = parse_llm_json(raw)
                if result and set(result) != {"raw_response"}:
                    return result
                last_error = f"invalid JSON from {candidate}: {raw[:200]}"
            except CreditsInsufficientError:
                raise
            except (RuntimeError, ValueError) as exc:
                last_error = f"{candidate}: {type(exc).__name__}: {exc}"
        raise RuntimeError(last_error or "vision recognize_json returned invalid JSON")

    async def recognize_bytes(
        self,
        prompt: str,
        blobs: Union[bytes, bytearray, Iterable[Union[bytes, bytearray]]],
        system: Optional[str] = None,
        temperature: float = 0.2,
        feature: str = "",
        profile: str = "auto",
        no_cache: bool = False,
        model: Optional[str] = None,
    ) -> str:
        """本地图片字节识别的语义化入口（等价 recognize + 自动 data URL 转换）。"""
        return await self.recognize(
            prompt, blobs, system=system, temperature=temperature,
            feature=feature, profile=profile, no_cache=no_cache, model=model,
        )


def get_vision_service() -> VisionService:
    return VisionService()


__all__ = [
    "VisionService",
    "get_vision_service",
    "sniff_image_mime",
    "to_data_url",
    "DEFAULT_VISION_MODEL",
]
