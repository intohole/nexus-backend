"""实时语音对话 WS 网关注册: register_voice_realtime_endpoints(app, handler, config)。

一行注册即接入，业务侧只需实现 VoiceTurnHandler；编排生命周期见 nexus.voice_session。
鉴权走首帧 hello {type:"auth",token}（5s 超时），token 不进 URL 避免访问日志泄漏。
"""
from __future__ import annotations

import asyncio
import json

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from nexus.logging import get_logger
from nexus.voice_session import VoiceRealtimeConfig, _RealtimeSession, VoiceTurnHandler

logger = get_logger("nexus.voice_realtime")

_AUTH_TIMEOUT_S = 5.0

__all__ = ["VoiceRealtimeConfig", "VoiceTurnHandler", "register_voice_realtime_endpoints"]


async def _default_authenticator(token: str, query: dict) -> dict:
    return {"user_id": "anonymous"} if token else {}


def register_voice_realtime_endpoints(
    app: FastAPI,
    handler: VoiceTurnHandler,
    config: VoiceRealtimeConfig,
    authenticator: Callable[[str, dict], Awaitable[dict | None]] | None = None,
    path: str = "/api/v1/voice/realtime",
) -> None:
    """注册 WS {path}。authenticator(token, query)->user|None，None 拒绝(4401)。

    user.session_id 非空时按其防双连：同 session_id 新连接踢除旧连接（superseded）。
    """
    auth = authenticator or _default_authenticator

    @app.websocket(path)
    async def voice_realtime_ws(websocket: WebSocket) -> None:
        query = dict(websocket.query_params)
        await websocket.accept()
        try:
            hello_raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect, Exception):
            await _reject(websocket, 4401, "auth timeout")
            return
        token = ""
        try:
            token = str(json.loads(hello_raw).get("token") or "")
        except ValueError:
            pass
        try:
            user = await auth(token, query)
        except Exception as e:
            logger.warning("voice ws auth error: %s", e)
            user = None
        if not user:
            await _reject(websocket, 4401, "unauthorized")
            return
        if len(_active_sessions) >= config.max_sessions:
            await _reject(websocket, 4429, "too many sessions")
            return
        sid = str(user.get("session_id") or "")
        old = _session_conns.get(sid) if sid else None
        if old is not None and not old.closed:
            await old.mark_superseded()
        session = _RealtimeSession(websocket, handler, config, dict(user))
        _active_sessions.add(session)
        if sid:
            _session_conns[sid] = session
        try:
            await session.run()
        finally:
            _active_sessions.discard(session)
            if sid and _session_conns.get(sid) is session:
                _session_conns.pop(sid, None)


async def _reject(websocket: WebSocket, code: int, reason: str) -> None:
    """关闭被拒连接的单源路径：已 accept 只 close，未 accept 先 accept 再 close。

    对已 accept 的 socket 二次 accept 会抛 RuntimeError——曾因此被 except pass 吞掉，
    close 永不执行，客户端悬死报 1006（S73 根治，禁回归 except pass）。
    """
    try:
        if websocket.client_state == WebSocketState.CONNECTING:
            await websocket.accept()
        await websocket.close(code=code, reason=reason)
    except Exception as e:
        logger.warning("voice ws reject close failed (code=%s): %s", code, e)


_active_sessions: set[_RealtimeSession] = set()
_session_conns: dict[str, _RealtimeSession] = {}
