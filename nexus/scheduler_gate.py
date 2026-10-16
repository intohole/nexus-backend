"""调度器启动门禁：非生产实例默认不得启动定时任务。

背景（LifeCompass r8）：本地 E2E 残留实例活了三天，其 cron 对着生产 notifyCenter
发出真实推送（Lion infra 配置会覆盖本地 env 指向，教训#117），受众是幽灵/已删账号。
业界同型护栏（Rails `Rails.env.production?` / Celery `ENABLE_BEAT`）：调度器只在被
显式允许时启动，缺省 fail-closed。

契约（显式参数 > 环境变量 > 默认）：
- mode="auto"（默认，向后兼容）：照常启动；
- mode="gated"：仅当 env SCHEDULER_ENABLED ∈ {1,true,yes,on} 才启动，否则拒绝；
- mode="off"：不启动。
mode 缺省时读 env NEXUS_SCHEDULER_MODE。拒绝时若进程带 APP_DIR（miniDeploy 托管特征）
升 ERROR——生产 cron 被门禁关掉必须响亮，不许静默。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

MODE_ENV = "NEXUS_SCHEDULER_MODE"
ENABLED_ENV = "SCHEDULER_ENABLED"
MANAGED_HINT_ENV = "APP_DIR"

MODE_AUTO = "auto"
MODE_GATED = "gated"
MODE_OFF = "off"
_MODES = (MODE_AUTO, MODE_GATED, MODE_OFF)
_TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    mode: str
    reason: str
    looks_managed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reason": self.reason,
            "looks_managed": self.looks_managed,
        }


def resolve_mode(mode: str | None) -> str:
    raw = (mode or os.getenv(MODE_ENV) or MODE_AUTO).strip().lower()
    return raw if raw in _MODES else MODE_AUTO


def scheduler_enabled_flag() -> bool:
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


def scheduler_gate(mode: str | None = None) -> GateDecision:
    resolved = resolve_mode(mode)
    managed = bool(os.getenv(MANAGED_HINT_ENV))
    if resolved == MODE_OFF:
        return GateDecision(False, resolved, "mode=off", managed)
    if resolved == MODE_GATED:
        if scheduler_enabled_flag():
            return GateDecision(True, resolved, f"{ENABLED_ENV} set", managed)
        return GateDecision(False, resolved, f"{ENABLED_ENV} unset", managed)
    return GateDecision(True, resolved, "auto", managed)


def log_gate_refusal(logger: logging.Logger, decision: GateDecision, scheduler_name: str) -> None:
    message = f"{scheduler_name} not started: mode={decision.mode} ({decision.reason})"
    if decision.looks_managed:
        logger.error(
            "%s — process looks miniDeploy-managed (%s set) yet scheduler is gated off; "
            "set %s=1 in the app env or production crons stay dead",
            message, MANAGED_HINT_ENV, ENABLED_ENV,
        )
        return
    logger.warning("%s — set %s=1 to run scheduled jobs in this process", message, ENABLED_ENV)


__all__ = [
    "GateDecision", "scheduler_gate", "resolve_mode", "scheduler_enabled_flag",
    "log_gate_refusal", "MODE_ENV", "ENABLED_ENV", "MODE_AUTO", "MODE_GATED", "MODE_OFF",
]
