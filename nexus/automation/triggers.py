"""内置触发器：定时（间隔/每日/ cron）与事件（手动触发）。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from nexus.automation.base import Trigger


def _as_datetime_at(value: str, now: datetime) -> datetime:
    hour, _, minute = value.partition(":")
    return now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


class ScheduleTrigger(Trigger):
    """配置：{"every":"minutes|hours|days","value":N,"at":"HH:MM"}
    - 不带 at：从 now 起间隔排程
    - 带 at（仅 days 或 hours 允许）：锚定到当天/下个整点的 HH:MM 排程
    或 {"cron":{"hour":9,"minute":0,"day_of_week":"mon-fri"}}
    """

    name = "schedule"

    def next_run_at(self, now: datetime, config: dict[str, Any]) -> Optional[datetime]:
        if "cron" in config:
            return self._next_cron(now, config["cron"])
        every = str(config.get("every") or "hours").lower()
        value = max(int(config.get("value") or 1), 1)
        at = config.get("at")
        if at and every in ("days", "hours"):
            anchor = _as_datetime_at(str(at), now)
            if every == "days":
                if anchor <= now:
                    anchor += timedelta(days=1)
                return anchor
            anchor = anchor.replace(minute=0, second=0, microsecond=0)
            while anchor <= now:
                anchor += timedelta(hours=value)
            anchor = anchor.replace(minute=int(str(at).partition(":")[2] or "0"))
            return anchor
        step = timedelta(minutes=value) if every == "minutes" else (
            timedelta(hours=value) if every == "hours" else timedelta(days=value)
        )
        return now + step

    @staticmethod
    def _next_cron(now: datetime, cron: dict[str, Any]) -> datetime:
        hour = int(cron.get("hour") or 0)
        minute = int(cron.get("minute") or 0)
        day = cron.get("day")
        if day is not None:
            target = int(day)
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate.day > target or (candidate.day == target and candidate <= now):
                if candidate.month == 12:
                    candidate = candidate.replace(year=candidate.year + 1, month=1)
                else:
                    candidate = candidate.replace(month=candidate.month + 1)
            try:
                return candidate.replace(day=target)
            except ValueError:
                return candidate + timedelta(days=1)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        dow = str(cron.get("day_of_week") or "")
        if not dow:
            return candidate
        mapper = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        if "-" in dow:
            start, _, end = dow.partition("-")
            allowed = list(range(mapper[start.strip()], mapper[end.strip()] + 1))
        else:
            allowed = [mapper[p.strip()] for p in re.split(r"[,\s]+", dow) if p.strip() in mapper]
        if not allowed:
            return candidate
        while candidate.weekday() not in allowed:
            candidate += timedelta(days=1)
        return candidate


class EventTrigger(Trigger):
    """事件型：next_run_at 为 None，仅通过手动触发执行，不入自动扫描。"""

    name = "event"

    def next_run_at(self, now: datetime, config: dict[str, Any]) -> Optional[datetime]:
        return None