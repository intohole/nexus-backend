"""tools×json_mode 互斥守底（r260 刀I N1）。

deepseek wire 上 response_format=json_object 与 function calling 组合行为未定义；
nexus 三消费面（主环/judge/抽取）均无此组合，命中即前置拦截——进 wire 前 raise，
不给「潜伏雷」留静默组合通道。
"""
from __future__ import annotations

import pytest

from nexus.llm import LLMService


def test_tools_json_mode_raises() -> None:
    core = LLMService()
    with pytest.raises(ValueError, match="tools 与 json_mode 互斥"):
        import asyncio

        asyncio.run(core.chat(messages=[{"role": "user", "content": "hi"}], json_mode=True, tools=[{"type": "function", "function": {"name": "f"}}]))


def test_tools_alone_and_json_alone_pass_gate() -> None:
    """互斥只拦组合：单独 tools 或单独 json_mode 不在此拦截（行为面各自既有链路）。"""
    core = LLMService()
    import asyncio

    # 单独 json_mode：进 _prepare_call 后的既有链路（网络面由既有测试覆盖），此处只验不抛互斥错
    with pytest.raises(Exception) as exc_info:
        asyncio.run(core.chat(messages=[{"role": "user", "content": "hi"}], json_mode=True))
    assert not isinstance(exc_info.value, ValueError) or "互斥" not in str(exc_info.value)
    with pytest.raises(Exception) as exc_info_tools:
        asyncio.run(core.chat(messages=[{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "f"}}]))
    assert not isinstance(exc_info_tools.value, ValueError) or "互斥" not in str(exc_info_tools.value)
