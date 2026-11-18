from __future__ import annotations

import asyncio

import pytest

from nexus.harness import LoopLimits, ToolSpec, parse_action, run_tool_loop


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _scripted(items: list[dict[str, object]]):
    rows = list(items)

    async def ask(prompt: str) -> dict[str, object]:
        return rows.pop(0)

    return ask


async def _tool(arg: str, state: str) -> str:
    return f"{arg}:{len(state)}"


def _tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec("node_check", "check", _tool),
        ToolSpec("skill", "load skill", _tool, requires_arg=True),
    )


def test_parse_rejects_unknown_action() -> None:
    tools = {tool.name: tool for tool in _tools()}
    with pytest.raises(ValueError):
        parse_action({"action": "write"}, tools, LoopLimits())


def test_parse_requires_state_on_final() -> None:
    tools = {tool.name: tool for tool in _tools()}
    with pytest.raises(ValueError):
        parse_action({"action": "final"}, tools, LoopLimits())


def test_parse_rejects_oversize_state() -> None:
    tools = {tool.name: tool for tool in _tools()}
    limits = LoopLimits(max_state_chars=10)
    with pytest.raises(ValueError):
        parse_action({"action": "revise", "state": "x" * 11}, tools, limits)


def test_parse_requires_tool_arg() -> None:
    tools = {tool.name: tool for tool in _tools()}
    with pytest.raises(ValueError):
        parse_action({"action": "skill"}, tools, LoopLimits())


def test_final_first_step() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([{"action": "final", "state": "a" * 60}]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(),
    ))
    assert outcome.stop_reason == "final"
    assert len(outcome.state) == 60


def test_tool_then_final_records_use() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([
            {"action": "skill", "arg": "levelgen"},
            {"action": "node_check"},
            {"action": "final", "state": "b" * 60},
        ]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(),
    ))
    assert outcome.tool_uses == ["skill", "node_check"]
    assert [step.action for step in outcome.steps] == ["skill", "node_check", "final"]


def test_revise_cap_rejects_then_final() -> None:
    rows = [{"action": "revise", "state": "c" * 60} for _ in range(4)]
    rows.append({"action": "final", "state": "c" * 60})
    outcome = _run(run_tool_loop(
        ask=_scripted(rows),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(max_steps=8, max_revise=3),
    ))
    assert outcome.revise_count == 3
    assert outcome.illegal_actions == 1
    assert any(step.action == "revise_rejected" for step in outcome.steps)
    assert outcome.stop_reason == "final"


def test_illegal_limit() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([{"action": "bogus"} for _ in range(3)]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(),
    ))
    assert outcome.stop_reason == "illegal_limit"
    assert outcome.illegal_actions == 3


def test_wall_limit() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([{"action": "final", "state": "a" * 60}]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(wall_limit_s=0.0),
    ))
    assert outcome.stop_reason == "wall_limit"
    assert outcome.state == "a" * 50


def test_ask_error_keeps_state() -> None:
    async def ask(prompt: str) -> dict[str, object]:
        raise RuntimeError("gateway down")

    outcome = _run(run_tool_loop(
        ask=ask,
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(),
    ))
    assert outcome.stop_reason == "ask_error"
    assert outcome.state == "a" * 50


def test_tool_result_truncated() -> None:
    async def long_tool(arg: str, state: str) -> str:
        return "z" * 5000

    outcome = _run(run_tool_loop(
        ask=_scripted([
            {"action": "node_check"},
            {"action": "final", "state": "a" * 60},
        ]),
        tools=(ToolSpec("node_check", "check", long_tool),),
        initial_state="a" * 50,
        limits=LoopLimits(max_result_chars=100),
    ))
    assert len(outcome.steps[0].result) == 100


def test_custom_state_field() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([{"action": "final", "logic_js": "q" * 60}]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(),
        state_field="logic_js",
    ))
    assert outcome.stop_reason == "final"
    assert outcome.state == "q" * 60


def test_repeat_limit_stops_no_progress() -> None:
    outcome = _run(run_tool_loop(
        ask=_scripted([{"action": "node_check"} for _ in range(4)]),
        tools=_tools(),
        initial_state="a" * 50,
        limits=LoopLimits(max_steps=6, max_repeat=2),
    ))
    assert outcome.stop_reason == "repeat_limit"
    assert outcome.tool_uses == ["node_check", "node_check", "node_check"]
