"""通用 Agent 工具循环原语：模型在协议内请求只读工具、替换状态、提交终稿。

业务侧只提供三件事：ask（一次模型调用）、tools（工具名+处理器）、render_prompt
（协议文本）。步数/修订/墙钟/非法动作/结果截断全部由本原语守底，失败一律显式
停止原因，不静默放行。readonly 为声明性元数据（观测面用），超时由调用方窗口控制。
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.harness")

AskFn = Callable[[str], Awaitable[dict[str, object]]]
ToolHandler = Callable[[str, str], Awaitable[str]]
StepHook = Callable[["LoopStep"], Awaitable[None]]
ToolProvider = Callable[[int], Sequence["ToolSpec"]]

RESERVED_ACTIONS = frozenset(("revise", "final"))
_ILLEGAL_ACTION = "illegal"
_REVISE_REJECTED = "revise_rejected"
_REPEAT_HINT = "连续相同调用无新信息，下一步请改用其他工具或 revise"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: ToolHandler
    readonly: bool = True
    requires_arg: bool = False


@dataclass(frozen=True)
class LoopLimits:
    max_steps: int = 6
    max_revise: int = 3
    wall_limit_s: float = 600.0
    max_state_chars: int = 60000
    min_state_chars: int = 1
    max_result_chars: int = 1600
    max_illegal: int = 3
    max_repeat: int = 2


@dataclass(frozen=True)
class LoopAction:
    kind: str
    arg: str = ""
    state: str = ""


@dataclass(frozen=True)
class LoopStep:
    step: int
    action: str
    arg: str
    result: str


@dataclass(frozen=True)
class LoopView:
    step: int
    limits: LoopLimits
    revised: int
    state: str
    history: tuple[str, ...]
    tools: tuple[ToolSpec, ...]
    state_field: str


@dataclass
class LoopOutcome:
    state: str
    steps: list[LoopStep] = field(default_factory=list)
    stop_reason: str = "final"
    illegal_actions: int = 0
    tool_uses: list[str] = field(default_factory=list)
    revise_count: int = 0
    wall_ms: int = 0


PromptRenderer = Callable[[LoopView], str]


@dataclass(frozen=True)
class LoopHooks:
    on_step: StepHook | None = None
    tools_for_step: ToolProvider | None = None


_TOOL_RESULT_FENCE_OPEN = "<<<TOOL_RESULT>>>"
_TOOL_RESULT_FENCE_CLOSE = "<<<END_TOOL_RESULT>>>"
_FENCE_TOKENS = ("<<<STATE>>>", "<<<END>>>", _TOOL_RESULT_FENCE_OPEN, _TOOL_RESULT_FENCE_CLOSE)


def _sanitize_tool_result(text: str) -> str:
    for token in _FENCE_TOKENS:
        text = text.replace(token, f"〈{token.strip('<>')}〉")
    return text


async def _emit_step(hooks: LoopHooks | None, step: LoopStep) -> bool:
    """hook 异常不击穿循环：返回 False 供调用方以 hook_error 停止并保留全部账本。"""
    if hooks is not None and hooks.on_step is not None:
        try:
            await hooks.on_step(step)
        except Exception as exc:
            logger.warning("on_step hook 异常，循环将以 hook_error 终止: %s", exc)
            return False
    return True


def parse_action(
    data: dict[str, object],
    tools: dict[str, ToolSpec],
    limits: LoopLimits,
    state_field: str = "state",
) -> LoopAction:
    kind = str(data.get("action") or "").strip()
    if kind not in tools and kind not in RESERVED_ACTIONS:
        raise ValueError(f"unknown action: {kind or '(empty)'}")
    raw_arg = data.get("arg")
    if isinstance(raw_arg, (dict, list)):
        arg = json.dumps(raw_arg, ensure_ascii=False)
    else:
        arg = str(raw_arg or "").strip()
    state = str(data.get(state_field) or "")
    if kind in RESERVED_ACTIONS:
        if len(state) < limits.min_state_chars:
            raise ValueError(f"{state_field} missing")
        if len(state) > limits.max_state_chars:
            raise ValueError(f"{state_field} {len(state)} > {limits.max_state_chars}")
    elif tools[kind].requires_arg and not arg:
        raise ValueError(f"{kind} arg missing")
    return LoopAction(kind=kind, arg=arg, state=state)


def render_default_prompt(view: LoopView) -> str:
    tools = "\n".join(
        f'{{"action":"{tool.name}"}} {tool.description}' for tool in view.tools
    )
    history = "\n".join(view.history[-6:]) or "（无）"
    return (
        f"# 工具循环（第 {view.step}/{view.limits.max_steps} 步；"
        f"已修订 {view.revised}/{view.limits.max_revise} 次）\n"
        "只输出一个 JSON 动作，不要输出解释或其他文字：\n"
        f"{tools}\n"
        f'{{"action":"revise","{view.state_field}":"完整新版本"}} 替换当前版本\n'
        f'{{"action":"final","{view.state_field}":"最终版本"}} 结束并提交\n'
        f"单次内容长度 {view.limits.min_state_chars}~{view.limits.max_state_chars} 字符。\n\n"
        f"# 最近工具结果\n{history}\n\n"
        f"# 当前版本（<<<STATE>>> 与 <<<END>>> 之间）\n"
        f"<<<STATE>>>\n{view.state}\n<<<END>>>\n"
    )


async def run_tool_loop(
    *,
    ask: AskFn,
    tools: Sequence[ToolSpec],
    initial_state: str,
    limits: LoopLimits,
    render_prompt: PromptRenderer | None = None,
    state_field: str = "state",
    hooks: LoopHooks | None = None,
) -> LoopOutcome:
    started = time.monotonic()
    renderer = render_prompt or render_default_prompt
    state = initial_state
    steps: list[LoopStep] = []
    history: list[str] = []
    tool_uses: list[str] = []
    illegal = 0
    revised = 0
    last_signature: str = ""
    repeat_count = 0

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    for step in range(1, limits.max_steps + 1):
        if time.monotonic() - started > limits.wall_limit_s:
            return LoopOutcome(state, steps, "wall_limit", illegal, tool_uses, revised, elapsed_ms())
        active_tools = tuple(hooks.tools_for_step(step)) if hooks and hooks.tools_for_step else tuple(tools)
        tool_map = {tool.name: tool for tool in active_tools}
        if not active_tools:
            history.append(f"[{step}] 本步无可用工具，请 revise 或 final 结束")
        view = LoopView(
            step=step,
            limits=limits,
            revised=revised,
            state=state,
            history=tuple(history),
            tools=active_tools,
            state_field=state_field,
        )
        try:
            data = await ask(renderer(view))
        except Exception as exc:
            history.append(f"[ask error] {str(exc)[:200]}")
            return LoopOutcome(state, steps, "ask_error", illegal, tool_uses, revised, elapsed_ms())
        try:
            action = parse_action(data, tool_map, limits, state_field)
        except ValueError as exc:
            illegal += 1
            detail = str(exc)[:200]
            history.append(f"[illegal] {detail}")
            step_record = LoopStep(step, _ILLEGAL_ACTION, "", detail)
            steps.append(step_record)
            if not await _emit_step(hooks, step_record):
                return LoopOutcome(state, steps, "hook_error", illegal, tool_uses, revised, elapsed_ms())
            if illegal >= limits.max_illegal:
                return LoopOutcome(state, steps, "illegal_limit", illegal, tool_uses, revised, elapsed_ms())
            continue
        if action.kind == "final":
            if action.state:
                state = action.state
            step_record = LoopStep(step, "final", "", f"{len(state)} chars")
            steps.append(step_record)
            if not await _emit_step(hooks, step_record):
                return LoopOutcome(state, steps, "hook_error", illegal, tool_uses, revised, elapsed_ms())
            return LoopOutcome(state, steps, "final", illegal, tool_uses, revised, elapsed_ms())
        if action.kind == "revise":
            if revised >= limits.max_revise:
                illegal += 1
                detail = f"revise rejected {revised}/{limits.max_revise}"
                history.append(f"[{step}] {detail}")
                step_record = LoopStep(step, _REVISE_REJECTED, "", detail)
                steps.append(step_record)
                if not await _emit_step(hooks, step_record):
                    return LoopOutcome(state, steps, "hook_error", illegal, tool_uses, revised, elapsed_ms())
                if illegal >= limits.max_illegal:
                    return LoopOutcome(state, steps, "illegal_limit", illegal, tool_uses, revised, elapsed_ms())
                continue
            state = action.state
            revised += 1
            detail = f"state replaced ({len(state)} chars)"
            history.append(f"[{step}] revise -> {detail}")
            step_record = LoopStep(step, "revise", "", detail)
            steps.append(step_record)
            if not await _emit_step(hooks, step_record):
                return LoopOutcome(state, steps, "hook_error", illegal, tool_uses, revised, elapsed_ms())
            continue
        tool = tool_map[action.kind]
        try:
            result = await tool.handler(action.arg, state)
        except Exception as exc:
            result = f"tool error: {str(exc)[:200]}"
        result = _sanitize_tool_result(result)
        if len(result) > limits.max_result_chars:
            result = result[: limits.max_result_chars] + f"…[已截断，共{len(result)}字符]"
        signature = f"{action.kind}|{action.arg}|{result}"
        if signature == last_signature:
            repeat_count += 1
            history.append(f"[{step}] hint: {_REPEAT_HINT}")
        else:
            repeat_count = 0
        last_signature = signature
        tool_uses.append(action.kind)
        history.append(
            f"[{step}] {action.kind} -> {_TOOL_RESULT_FENCE_OPEN}\n{result}\n{_TOOL_RESULT_FENCE_CLOSE}"
        )
        step_record = LoopStep(step, action.kind, action.arg, result)
        steps.append(step_record)
        if not await _emit_step(hooks, step_record):
            return LoopOutcome(state, steps, "hook_error", illegal, tool_uses, revised, elapsed_ms())
        if limits.max_repeat > 0 and repeat_count >= limits.max_repeat:
            return LoopOutcome(
                state, steps, "repeat_limit", illegal, tool_uses, revised, elapsed_ms())
    return LoopOutcome(state, steps, "max_steps", illegal, tool_uses, revised, elapsed_ms())
