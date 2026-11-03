"""P3 收归守底：asr/TTS 流式客户端 close 生命周期单源于 _WsStreamCloser。

dup_block_scan 曾实测两处 17 行逐行同构克隆；本守底钉死收归态，
再回潮双写（任一类重写 close 脱离基座）即红。
"""
import pytest

from nexus._ws_stream_base import _WsStreamCloser
from nexus.asr import AsrStreamSession
from nexus.volc import TTSStreamSession


def test_stream_sessions_share_close_lifecycle() -> None:
    assert issubclass(AsrStreamSession, _WsStreamCloser)
    assert issubclass(TTSStreamSession, _WsStreamCloser)
    assert AsrStreamSession.close is _WsStreamCloser.close
    assert TTSStreamSession.close is _WsStreamCloser.close


@pytest.mark.asyncio
async def test_close_idempotent_without_connection() -> None:
    for session in (AsrStreamSession(api_key="k"), TTSStreamSession(api_key="k")):
        await session.close()
        await session.close()
        assert session._closed is True
        assert session._reader_task is None
        assert session._ws is None
