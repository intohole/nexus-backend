"""nexus.streaming 帧原语契约测试。"""
import json

from nexus.streaming import (
    SSE_DONE,
    SSE_HEADERS,
    sse_data_line,
    sse_event_dict,
    sse_raw_frame,
)


def test_sse_done_sentinel():
    assert SSE_DONE == "data: [DONE]\n\n"


def test_sse_data_line_default():
    frame = sse_data_line({"type": "done", "content": "ok"})
    assert frame == 'data: {"type": "done", "content": "ok"}\n\n'
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"


def test_sse_data_line_event_prefix():
    frame = sse_data_line({"plan": {"a": 1}}, event="done")
    assert frame.startswith("event: done\ndata: ")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("\n", 1)[1][len("data: "):])
    assert payload == {"plan": {"a": 1}}


def test_sse_data_line_non_ascii():
    assert "计划" in sse_data_line({"message": "计划生成中"})


def test_sse_raw_frame_passthrough():
    raw = json.dumps({"ping": True})
    assert sse_raw_frame(raw) == f"data: {raw}\n\n"


def test_sse_event_dict_injects_type():
    frame = sse_event_dict("delta", {"content": "hi"})
    assert json.loads(frame[len("data: "):]) == {"type": "delta", "content": "hi"}
