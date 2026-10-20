"""工具结果三态原语（dsh v0.2.0-rc.1 语义收归，harness 调研 A1/backlog#11）。

success / known-failed / unknown 三态：known-failed 走调用方既有降级路径；
unknown（超时/连接中断=结果未知）不得盲目重试——写副作用工具可能已在服务端
生效，须先核实副作用再决定下一步。只读工具（readonly=True）超时重试一次
无副作用风险，可保留。消费方：nexus-agent agent_react 超时分支；新增 agent
循环按 handoff/02 harness 纪律表接同一原语。
"""
from __future__ import annotations

UNKNOWN_OUTCOME_HINT = (
    "能力 {action} 执行超时(超过{timeout}秒)，本次调用结果未知：该能力可能是写操作，"
    "上一次调用可能已在服务端生效。请勿重复调用同一能力(避免重复创建/修改)，"
    "可改用查询类能力核实其是否已生效；无法核实时，如实告知用户结果状态未知、"
    "建议稍后到对应应用核对，严禁声称已成功或编造数据。"
)


def tool_outcome_kind(timed_out: bool, tool_result: object) -> str:
    if timed_out:
        return "unknown"
    if isinstance(tool_result, dict) and tool_result.get("error"):
        return "known-failed"
    return "success"


def unknown_outcome_hint(action: str, timeout_seconds: object) -> str:
    return UNKNOWN_OUTCOME_HINT.format(action=action, timeout=int(timeout_seconds or 0))
