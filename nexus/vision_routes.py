"""通用图片识别路由工厂 — 业务应用一行挂载标准识别端点.

用法（业务后端 main.py）::

    from nexus.vision_routes import create_vision_router

    app.include_router(create_vision_router())

端点：
- ``POST {prefix}/recognize``：multipart 上传图片 + prompt，返回识别文本或 JSON
- ``GET  {prefix}/model``：当前视觉模型名（供前端展示）

图片以 base64 data URL 直传视觉网关，不做磁盘落盘；类型白名单
png/jpeg/webp/gif/bmp，默认单张 ≤10MB、最多 4 张。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from nexus.auth import get_current_user_id_required
from nexus.response import success_response
from nexus.vision import get_vision_service, sniff_image_mime

ALLOWED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp"}


def create_vision_router(
    *,
    prefix: str = "/api/vision",
    tags: list[str] | None = None,
    max_images: int = 4,
    max_size_mb: float = 10,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=tags or ["vision"])
    max_bytes = int(max_size_mb * 1024 * 1024)

    async def _read_images(files: list[UploadFile]) -> list[bytes]:
        if not files:
            raise HTTPException(status_code=400, detail="请至少上传一张图片")
        if len(files) > max_images:
            raise HTTPException(status_code=400, detail=f"最多支持 {max_images} 张图片")
        blobs: list[bytes] = []
        for file in files:
            declared = file.content_type or ""
            if declared and declared not in ALLOWED_IMAGE_MIMES:
                raise HTTPException(status_code=400, detail=f"不支持的图片类型: {declared}")
            chunks: list[bytes] = []
            size = 0
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail=f"图片超过 {max_size_mb}MB 大小限制")
                chunks.append(chunk)
            blob = b"".join(chunks)
            if not blob:
                raise HTTPException(status_code=400, detail="图片内容为空")
            try:
                sniff_image_mime(blob)
            except ValueError:
                raise HTTPException(status_code=400, detail="图片格式无法识别（仅支持 png/jpeg/webp/gif/bmp）")
            blobs.append(blob)
        return blobs

    @router.post("/recognize")
    async def recognize(
        files: list[UploadFile] = File(..., description="图片文件，可多张"),
        prompt: str = Form("", description="识别要求，如「识别图中物品并列出名称」"),
        system: str | None = Form(None, description="可选 system 提示词"),
        json_mode: bool = Form(False, description="true 时返回结构化 JSON"),
        _user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise HTTPException(status_code=400, detail="prompt 不能为空")
        blobs = await _read_images(files)
        service = get_vision_service()
        if json_mode:
            result = await service.recognize_json(prompt, blobs, system=system)
            return success_response({"result": result, "model": service.model})
        content = await service.recognize(prompt, blobs, system=system)
        return success_response({"content": content, "model": service.model})

    @router.get("/model")
    async def vision_model(
        _user_id: str = Depends(get_current_user_id_required),
    ) -> dict[str, Any]:
        service = get_vision_service()
        return success_response({"model": service.model})

    return router


__all__ = ["create_vision_router", "ALLOWED_IMAGE_MIMES"]
