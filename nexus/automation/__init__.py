"""自动化引擎中间件：策略接口 + 内置触发器 + 注册表 + 编排引擎。

中间件不参与数据库行为：不建表、不读写业务数据。规则行建模与持久化、
具体动作实现均由消费项目承担。
"""
from nexus.automation.base import (
    AutomationContext,
    AutomationResult,
    Action,
    Condition,
    Trigger,
)
from nexus.automation.engine import AutomationEngine, RunOutcome
from nexus.automation.registry import StrategyRegistry
from nexus.automation.triggers import EventTrigger, ScheduleTrigger

__all__ = [
    "AutomationContext",
    "AutomationResult",
    "Action",
    "Condition",
    "Trigger",
    "AutomationEngine",
    "RunOutcome",
    "StrategyRegistry",
    "EventTrigger",
    "ScheduleTrigger",
]