"""自动化编排引擎：扫描到期规则、解析条件、执行动作、计算下次排程。

引擎不接触数据库：规则行由消费项目注入（scan 传入），执行结果与
next_run_at 由项目负责持久化。上下文中的业务能力同样由项目注入。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from nexus.automation.base import AutomationContext, Action, Condition, Trigger
from nexus.automation.registry import StrategyRegistry
from nexus.logging import get_logger

logger = get_logger("nexus.automation")

ContextFactory = Callable[[Any, datetime], AutomationContext]


@dataclass
class RunOutcome:
    rule_id: int
    status: str
    ok: bool
    message: str
    next_run_at: Optional[datetime]


class AutomationEngine:
    def __init__(self, retry_minutes: int = 15) -> None:
        self.retry_minutes = retry_minutes
        self.triggers: StrategyRegistry[Trigger] = StrategyRegistry("触发器")
        self.conditions: StrategyRegistry[Condition] = StrategyRegistry("条件")
        self.actions: StrategyRegistry[Action] = StrategyRegistry("动作")

    def register_trigger(self, trigger: Trigger) -> Trigger:
        return self.triggers.register(trigger)

    def register_condition(self, condition: Condition) -> Condition:
        return self.conditions.register(condition)

    def register_action(self, action: Action) -> Action:
        return self.actions.register(action)

    async def run_rule(
        self,
        rule: Any,
        ctx_factory: ContextFactory,
        now: Optional[datetime] = None,
    ) -> RunOutcome:
        now = now or datetime.now()
        trigger = self.triggers.get(getattr(rule, "trigger_type", None))
        if trigger is None:
            return RunOutcome(rule.id, "no_trigger", False, f"触发器未注册: {getattr(rule, 'trigger_type', '')}", now + timedelta(minutes=self.retry_minutes))
        action = self.actions.get(getattr(rule, "action_type", None))
        if action is None:
            return RunOutcome(rule.id, "no_action", False, f"动作未注册: {getattr(rule, 'action_type', '')}", now + timedelta(minutes=self.retry_minutes))
        ctx = ctx_factory(rule, now)
        condition = self.conditions.get(getattr(rule, "condition_type", None))
        if condition is not None:
            try:
                if not await condition.apply(ctx):
                    next_run = trigger.next_run_at(now, rule.trigger_config)
                    return RunOutcome(rule.id, "condition_false", True, "条件不满足，跳过执行", next_run)
            except Exception as e:
                next_run = now + timedelta(minutes=self.retry_minutes)
                logger.warning("automation condition error rule=%s: %s", rule.id, e)
                return RunOutcome(rule.id, "condition_error", False, f"条件执行异常: {e}", next_run)
        try:
            result = await action.execute(ctx)
        except Exception as e:
            logger.warning("automation action error rule=%s: %s", rule.id, e)
            return RunOutcome(rule.id, "error", False, f"执行异常: {e}", now + timedelta(minutes=self.retry_minutes))
        if result.next_run_at is not None:
            next_run = result.next_run_at
        elif result.ok:
            next_run = trigger.next_run_at(now, rule.trigger_config)
        else:
            next_run = now + timedelta(minutes=self.retry_minutes)
        return RunOutcome(rule.id, "executed", result.ok, result.message, next_run)

    async def run_now(
        self,
        rule: Any,
        ctx_factory: ContextFactory,
        now: Optional[datetime] = None,
    ) -> RunOutcome:
        """手动/事件触发：无视 next_run_at 立即执行一次，成功后按触发器推进。"""
        return await self.run_rule(rule, ctx_factory, now)

    async def scan(
        self,
        rules: list[Any],
        ctx_factory: ContextFactory,
        now: Optional[datetime] = None,
    ) -> list[RunOutcome]:
        now = now or datetime.now()
        outcomes: list[RunOutcome] = []
        for rule in rules:
            next_run = getattr(rule, "next_run_at", None)
            if next_run is None or next_run > now:
                continue
            outcomes.append(await self.run_rule(rule, ctx_factory, now))
        return outcomes