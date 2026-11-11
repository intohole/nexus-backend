"""nexus.memory 原语守底：信封渲染分型、字节稳定、fail-open 辅助。"""
import asyncio

import pytest

from nexus.memory import (
    empty_reason_of,
    recall_context,
    render_memory_context,
    render_working_context,
    working_context,
)

RECALL_ENVELOPE = {
    "memories": [
        {
            "id": "m1",
            "content": "用户对花生过敏",
            "source_app": "aipet",
            "score": 0.83,
            "superseded": False,
            "revived": False,
            "created_at": "2026-10-28T09:30:00",
        },
        {
            "id": "m2",
            "content": "用户在规划北海道旅行",
            "source_app": "nexus-agent",
            "score": 0.71,
            "superseded": True,
            "revived": False,
            "created_at": "2026-10-01T12:00:00",
        },
        {
            "id": "m3",
            "content": "用户偏好简洁回答",
            "source_app": "",
            "score": 0.66,
            "superseded": False,
            "revived": True,
            "created_at": "2026-09-15T08:00:00",
        },
    ],
    "count": 3,
    "analysis": {"trigger": "direct"},
    "context_chars": 120,
    "max_chars": 2000,
}

SNAPSHOT_ENVELOPE = {
    "memories": [
        {"id": "s1", "content": "用户是后端工程师", "source_app": "aipet", "memory_type": "semantic", "created_at": "2026-10-28T09:30:00"}
    ],
    "count": 1,
    "context_chars": 30,
}


def test_render_recall_block_structured():
    block = render_memory_context(RECALL_ENVELOPE, self_app="nexus-agent")
    assert block.startswith("以下是用户的历史记忆（命中 3 条）")
    assert "- 用户对花生过敏（来自应用「aipet」·2026-10-28）" in block
    assert "- 用户在规划北海道旅行（2026-10-01·已被用户后来的表述更新）" in block
    assert "- 用户偏好简洁回答（2026-09-15·重新启用的较早记忆）" in block
    assert block.endswith("以本轮表述为准。")
    assert "0.83" not in block


def test_render_snapshot_kind_autodetect():
    block = render_memory_context(SNAPSHOT_ENVELOPE)
    assert block.startswith("以下是用户的长期记忆画像（最相关 1 条）")
    assert "来自应用「aipet」" in block


def test_render_byte_stable():
    first = render_memory_context(RECALL_ENVELOPE, self_app="nexus-agent")
    second = render_memory_context(RECALL_ENVELOPE, self_app="nexus-agent")
    assert first == second
    assert "命中 3 条" in first


def test_render_empty_envelopes_return_empty_string():
    assert render_memory_context({"memories": [], "count": 0, "empty_reason": "no_scope", "hint": "用户暂无记忆"}) == ""
    assert render_memory_context({"success": False, "unavailable": True, "message": "HTTP 404"}) == ""
    assert render_memory_context({"memories": [], "count": 0, "error": "boom"}) == ""
    assert render_memory_context({}) == ""


def test_render_skips_blank_content_items():
    payload = {"memories": [{"id": "x", "content": "  "}], "count": 1}
    assert render_memory_context(payload) == ""


def test_empty_reason_of():
    assert empty_reason_of(RECALL_ENVELOPE) == ""
    assert empty_reason_of({"memories": [], "empty_reason": "no_match"}) == "no_match"
    assert empty_reason_of({"memories": [], "count": 0}) == ""


class _FakeSDK:
    def __init__(self, result=None, error=False):
        self.result = result if result is not None else dict(RECALL_ENVELOPE)
        self.error = error
        self.calls = []

    async def recall_memories(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError("boom")
        return self.result


def test_recall_context_success_renders_block():
    sdk = _FakeSDK()
    block = asyncio.run(
        recall_context("u1", "我能吃什么", sdk=sdk, app_name="aipet", self_app="aipet")
    )
    assert "用户对花生过敏" in block
    assert sdk.calls[0]["user_id"] == "u1"
    assert sdk.calls[0]["app_name"] == "aipet"


def test_recall_context_guest_and_blank_short_circuit():
    sdk = _FakeSDK()
    assert asyncio.run(recall_context("guest", "你好", sdk=sdk)) == ""
    assert asyncio.run(recall_context("u1", "  ", sdk=sdk)) == ""
    assert sdk.calls == []


def test_recall_context_fail_open():
    assert asyncio.run(recall_context("u1", "问句", sdk=_FakeSDK(error=True))) == ""
    assert asyncio.run(
        recall_context("u1", "问句", sdk=_FakeSDK(result={"success": False, "unavailable": True, "message": "x"}))
    ) == ""
    assert asyncio.run(
        recall_context("u1", "问句", sdk=_FakeSDK(result={"memories": [], "count": 0, "empty_reason": "no_scope"}))
    ) == ""


@pytest.mark.asyncio
async def test_recall_context_no_scope_logs(caplog):
    sdk = _FakeSDK(result={"memories": [], "count": 0, "empty_reason": "no_scope", "hint": "无记忆"})
    with caplog.at_level("INFO", logger="nexus.memory"):
        block = await recall_context("u1", "问句", sdk=sdk)
    assert block == ""
    assert any("no_scope" in r.message for r in caplog.records)


CONTEXT_ENVELOPE = {
    "history_window": [
        {"role": "user", "content": "我下周要去北海道", "timestamp": "2026-11-11T08:00:00"},
        {"role": "assistant", "content": "好的，需要我推荐行程吗", "timestamp": "2026-11-11T08:00:05"},
    ],
    "history_count": 2,
    "history_chars": 40,
    "history_total": 6,
    "memories": [
        {"id": "c1", "content": "用户喜欢滑雪", "source_app": "nexus-agent", "created_at": "2026-11-01T10:00:00"},
        {"id": "c2", "content": "用户有花粉过敏史", "source_app": "aipet", "created_at": "2026-10-20T09:00:00"},
    ],
    "count": 2,
    "context_chars": 50,
    "total_chars": 90,
}


def test_render_working_context_two_sections():
    block = render_working_context(CONTEXT_ENVELOPE, self_app="nexus-agent")
    assert block.startswith("以下是用户近期对话的记录（2 条）")
    assert "- user: 我下周要去北海道" in block
    assert "- assistant: 好的，需要我推荐行程吗" in block
    assert "以下是用户的长期记忆画像（最相关 2 条）" in block
    assert "- 用户喜欢滑雪" in block, "同源记忆不标注来源"
    assert "- 用户有花粉过敏史（来自应用「aipet」·2026-10-20）" in block
    assert block.endswith("以本轮表述为准。")
    assert "08:00:05" not in block, "字节稳定纪律：块内不含时变值"


def test_render_working_context_history_only_or_memory_only():
    hist_only = render_working_context({"history_window": CONTEXT_ENVELOPE["history_window"], "memories": []})
    assert "近期对话的记录" in hist_only and "长期记忆画像" not in hist_only
    mem_only = render_working_context({"history_window": [], "memories": CONTEXT_ENVELOPE["memories"]})
    assert "长期记忆画像" in mem_only and "近期对话的记录" not in mem_only


def test_render_working_context_empty_envelopes_return_empty():
    assert render_working_context({"history_window": [], "memories": []}) == ""
    assert render_working_context({"success": False, "unavailable": True, "message": "x"}) == ""
    assert render_working_context({}) == ""
    assert render_working_context({"history_window": [], "count": 0, "error": "boom"}) == ""


class _CtxFakeSDK:
    def __init__(self, result=None, error=False):
        self.result = result if result is not None else dict(CONTEXT_ENVELOPE)
        self.error = error
        self.calls = []

    async def get_context(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise RuntimeError("boom")
        return self.result


def test_working_context_success_and_args_passthrough():
    sdk = _CtxFakeSDK()
    block = asyncio.run(
        working_context("u1", sdk=sdk, app_name="aipet", session_id="s1", history_turns=8)
    )
    assert "我下周要去北海道" in block
    assert sdk.calls[0]["user_id"] == "u1"
    assert sdk.calls[0]["session_id"] == "s1"
    assert sdk.calls[0]["history_turns"] == 8


def test_working_context_guest_and_fail_open():
    sdk = _CtxFakeSDK()
    assert asyncio.run(working_context("guest", sdk=sdk)) == ""
    assert asyncio.run(working_context("", sdk=sdk)) == ""
    assert sdk.calls == [], "guest/空用户短路不打 SDK"
    assert asyncio.run(working_context("u1", sdk=_CtxFakeSDK(error=True))) == ""
    assert asyncio.run(working_context("u1", sdk=_CtxFakeSDK(result={"history_window": [], "memories": []}))) == ""
