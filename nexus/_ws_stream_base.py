"""火山 WS 流式客户端公共生命周期：reader 任务收割 + ws 关闭单源。

AsrStreamSession 与 TTSStreamSession 的 close 原为逐行同构克隆（dup_block_scan
23 行簇实测 17 行），收归本基座防同构缺陷双写漂移；两客户端各自在 __init__
里持有 _closed/_reader_task/_ws 三件，本类只提供拆卸时序。
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional


class _WsStreamCloser:
    _reader_task: Optional["asyncio.Task"] = None
    _ws: Optional[Any] = None
    _closed: bool = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
