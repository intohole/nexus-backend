"""审批状态机：审批状态定义、流转规则与审批动作执行。"""
from __future__ import annotations

from enum import Enum


class ApprovalStatus(str, Enum):
    DRAFT = "draft"
    PENDING = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


_TRANSITIONS: dict[ApprovalStatus, set[ApprovalStatus]] = {
    ApprovalStatus.DRAFT: {ApprovalStatus.PENDING},
    ApprovalStatus.PENDING: {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.DRAFT},
    ApprovalStatus.APPROVED: {ApprovalStatus.DRAFT, ApprovalStatus.PENDING},
    ApprovalStatus.REJECTED: {ApprovalStatus.DRAFT, ApprovalStatus.PENDING},
}


class ApprovalStateMachine:
    def __init__(self) -> None:
        self._transitions: dict[ApprovalStatus, set[ApprovalStatus]] = _TRANSITIONS

    def can_transition(self, current: ApprovalStatus, target: ApprovalStatus) -> bool:
        return target in self._transitions.get(current, set())

    def next_status(self, current: ApprovalStatus, action: str) -> ApprovalStatus | None:
        action_map: dict[str, ApprovalStatus] = {
            "submit": ApprovalStatus.PENDING,
            "approve": ApprovalStatus.APPROVED,
            "reject": ApprovalStatus.REJECTED,
            "revise": ApprovalStatus.DRAFT,
        }
        target = action_map.get(action)
        if target is None or not self.can_transition(current, target):
            return None
        return target

    def suggest_next(self, current: ApprovalStatus) -> list[str]:
        actions = []
        if current in (ApprovalStatus.DRAFT, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            actions.append("submit")
        if current == ApprovalStatus.PENDING:
            actions.extend(["approve", "reject"])
        if current in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            actions.append("revise")
        return actions


_state_machine: ApprovalStateMachine | None = None


def get_approval_machine() -> ApprovalStateMachine:
    global _state_machine
    if _state_machine is None:
        _state_machine = ApprovalStateMachine()
    return _state_machine


async def apply_approval_action(
    current: str,
    action: str,
    transition_cb: object | None = None,
) -> tuple[str, str]:
    machine = get_approval_machine()
    try:
        cur = ApprovalStatus(current)
    except ValueError:
        return "invalid_status", current
    target = machine.next_status(cur, action)
    if target is None:
        return "invalid_transition", current
    if transition_cb is not None:
        await transition_cb(target.value)
    return "ok", target.value