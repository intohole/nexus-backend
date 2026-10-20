import pytest

from nexus.volc import (
    TTSFrame,
    VolcArkClient,
    VolcArkUnavailableError,
    build_tts_frame,
    parse_tts_frame,
)


def test_build_frame_start_connection_has_no_session_id() -> None:
    raw = build_tts_frame(1, "sid-unused", b"{}")
    assert raw[0] == 0x11
    assert raw[1] == 0x14
    assert raw[2] == 0x10
    assert int.from_bytes(raw[4:8], "big") == 1
    size = int.from_bytes(raw[8:12], "big")
    assert raw[12:12 + size] == b"{}"
    assert len(raw) == 12 + size


def test_build_frame_session_carries_session_id() -> None:
    raw = build_tts_frame(100, "sess-123", b"{}")
    event = int.from_bytes(raw[4:8], "big")
    sid_size = int.from_bytes(raw[8:12], "big")
    assert event == 100
    assert sid_size == 8
    assert raw[12:20] == b"sess-123"
    size = int.from_bytes(raw[20:24], "big")
    assert raw[24:24 + size] == b"{}"


def test_roundtrip_full_client_request() -> None:
    raw = build_tts_frame(200, "s1", b'{"k":1}')
    frame = parse_tts_frame(raw)
    assert frame.msg_type == 0b0001
    assert frame.event == 200
    assert frame.session_id == "s1"
    assert frame.payload == b'{"k":1}'


def test_parse_error_frame() -> None:
    body = b'{"error":"decode failed"}'
    raw = (
        bytes([0x11, 0xF0, 0x10, 0x00])
        + (50001).to_bytes(4, "big")
        + len(body).to_bytes(4, "big")
        + body
    )
    frame = parse_tts_frame(raw)
    assert frame.msg_type == 0b1111
    assert frame.error_code == 50001
    assert frame.payload == body


def test_parse_audio_frame() -> None:
    audio = b"\x00\x01\x02mp3data"
    raw = bytes([0x11, 0xB0, 0x10, 0x00]) + len(audio).to_bytes(4, "big") + audio
    frame = parse_tts_frame(raw)
    assert frame.msg_type == 0b1011
    assert frame.payload == audio


def test_parse_connection_started_with_connect_id() -> None:
    cid = b"conn-abc"
    inner = (50).to_bytes(4, "big") + len(cid).to_bytes(4, "big") + cid
    body = b"{}"
    raw = (
        bytes([0x11, 0x94, 0x10, 0x00])
        + inner
        + len(body).to_bytes(4, "big")
        + body
    )
    frame = parse_tts_frame(raw)
    assert frame.msg_type == 0b1001
    assert frame.event == 50
    assert frame.session_id == ""
    assert frame.payload == body


@pytest.mark.asyncio
async def test_client_requires_ark_key() -> None:
    client = VolcArkClient(ark_api_key="")
    from pathlib import Path

    with pytest.raises(VolcArkUnavailableError):
        await client.generate_image("p", Path("/tmp/x.png"))


@pytest.mark.asyncio
async def test_client_requires_tts_key() -> None:
    client = VolcArkClient(tts_api_key="")
    from pathlib import Path

    with pytest.raises(VolcArkUnavailableError):
        await client.synthesize_speech("t", Path("/tmp/x.mp3"))


def test_frame_type_bytes_documented() -> None:
    assert TTSFrame(msg_type=0b1011).msg_type == 0b1011
