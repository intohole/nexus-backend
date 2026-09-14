"""自动化引擎抽象：Trigger/Action/Condition 策略接口与上下文。

中间件不参与任何数据库行为：不建表、不读写业务数据，规则行由消费项目
自行建模并提供（RuleProtocol 鸭子类型）。引擎只负责编排：判定到期、
解析条件、执行动作、计算下一次执行时间。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class AutomationContext:
    rule: Any
    now: datetime
    scope: dict[str, Any] = field(default_factory=dict)


@dataclass
class AutomationResult:
    ok: bool = True
    message: str = ""
    next_run_at: Optional[datetime] = None


class Trigger(ABC):
    name: str = ""

    @abstractmethod
    def next_run_at(self, now: datetime, config: dict[str, Any]) -> Optional[datetime]:
        """计算基于 now 的下一次执行时间；返回 None 表示一次性（执行后不再排程）。"""


class Condition(ABC):
    name: str = ""

    @abstractmethod
    async def apply(self, ctx: AutomationContext) -> bool:
        """条件判定：为 False 时跳过动作但正常推进排程。"""


class Action(ABC):
    name: str = ""

    @abstractmethod
    async def execute(self, ctx: AutomationContext) -> AutomationResult:
        """执行动作：具体业务能力由消费项目实现并注入。"""