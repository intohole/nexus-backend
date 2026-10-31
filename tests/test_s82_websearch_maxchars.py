"""S82 上下文窗口守底：search_with_context 可选 max_chars（默认 None 零行为变化）。"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.web_search import get_web_search_service

_RESULTS = [
    {"title": "t1", "content": "c" * 500, "media": "", "link": ""},
    {"title": "t2", "content": "c" * 500, "media": "m2", "link": "http://x"},
]


@pytest.fixture()
def stubbed_search(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(self: Any, query: str, count: int, recency: str | None = None) -> list[dict[str, str]]:
        return _RESULTS

    monkeypatch.setattr(type(get_web_search_service()), "search", _search)


def test_default_none_keeps_legacy_behavior(stubbed_search: None) -> None:
    out = _run("q")
    assert out == "1. t1\n   " + "c" * 500 + "\n2. t2 (m2)\n   " + "c" * 500 + "\n   来源: http://x"


def test_max_chars_truncates_content(stubbed_search: None) -> None:
    out = _run("q", max_chars=10)
    assert "c" * 11 not in out, "超出 max_chars 的内容必须被截断"
    assert "c" * 10 in out, "截断到恰好 max_chars"
    assert out.count("\n   来源: http://x") == 1, "link 行不受截断影响"


def test_max_chars_zero_or_negative_is_noop(stubbed_search: None) -> None:
    assert _run("q", max_chars=0) == _run("q")
    assert _run("q", max_chars=-5) == _run("q")


def _run(query: str, **kwargs: Any) -> str:
    import asyncio

    async def go() -> str:
        return await get_web_search_service().search_with_context(query, **kwargs)

    return asyncio.run(go())
