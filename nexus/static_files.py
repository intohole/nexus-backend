"""静态资源装配：/static 挂载与可选 SPA fallback 路由。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nexus.config import get_settings


def setup_static_files(
    app: FastAPI,
    directory: Optional[str] = None,
    spa_fallback: Optional[bool] = None,
    path_prefix: Optional[str] = None,
) -> None:
    cfg = get_settings()
    static_dir: str = directory or cfg.static_files.directory
    spa: bool = spa_fallback if spa_fallback is not None else cfg.static_files.spa_fallback
    prefix: str = cfg.path_prefix if path_prefix is None else (path_prefix or "")

    static_path: Path = Path(static_dir)
    if not static_path.exists():
        static_path.mkdir(parents=True, exist_ok=True)

    mount_path: str = f"{prefix}/static" if prefix else "/static"
    app.mount(mount_path, StaticFiles(directory=str(static_path)), name="static")

    if spa:
        from nexus.middleware_exception import not_found_response

        index_path: Path = static_path / "index.html"
        static_resolved: Path = static_path.resolve()

        def _not_found(request: Request) -> HTMLResponse | JSONResponse:
            return not_found_response(request)

        @app.get(
            f"{prefix}/{{full_path:path}}" if prefix else "/{full_path:path}",
            response_model=None,
        )
        async def spa_fallback_route(full_path: str, request: Request) -> HTMLResponse | JSONResponse | FileResponse:
            file_path: Path = static_path / full_path
            try:
                resolved: Path = file_path.resolve()
            except (OSError, ValueError):
                return _not_found(request)
            if not resolved.is_relative_to(static_resolved):
                return _not_found(request)
            if resolved.exists() and resolved.is_file():
                return FileResponse(str(resolved))
            if index_path.exists():
                return FileResponse(str(index_path))
            return _not_found(request)
