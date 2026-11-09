"""nexus.automation ScheduleTrigger 守底（nexus-agent r32 迁移对表供料）。

核心回归：cron day 短月滚月语义——月任务 day=31 在 2 月必须跳 3/31，
不得「+1 天」天天触发（r32 映射表实锤的双重副作用灾难）。
"""
from datetime import datetime

from nexus.automation.triggers import ScheduleTrigger


def _next(now: datetime, config: dict) -> datetime:
    return ScheduleTrigger().next_run_at(now, config)


def test_monthly_day31_in_feb_rolls_to_march():
    got = _next(datetime(2026, 2, 1, 10, 0), {"cron": {"hour": 8, "minute": 0, "day": 31}})
    assert got == datetime(2026, 3, 31, 8, 0)


def test_monthly_day31_within_month():
    got = _next(datetime(2026, 1, 15, 10, 0), {"cron": {"hour": 8, "minute": 0, "day": 31}})
    assert got == datetime(2026, 1, 31, 8, 0)


def test_monthly_day30_in_feb_rolls_twice_to_march():
    got = _next(datetime(2026, 2, 5, 10, 0), {"cron": {"hour": 8, "minute": 0, "day": 30}})
    assert got == datetime(2026, 3, 30, 8, 0)


def test_monthly_day31_after_fired_in_march_skips_april():
    got = _next(datetime(2026, 3, 31, 9, 0), {"cron": {"hour": 8, "minute": 0, "day": 31}})
    assert got == datetime(2026, 5, 31, 8, 0)


def test_monthly_day15_normal_month():
    got = _next(datetime(2026, 4, 1, 6, 0), {"cron": {"hour": 8, "minute": 0, "day": 15}})
    assert got == datetime(2026, 4, 15, 8, 0)


def test_daily_anchor_at():
    got = _next(datetime(2026, 11, 10, 7, 0), {"every": "days", "value": 1, "at": "08:00"})
    assert got == datetime(2026, 11, 10, 8, 0)
    got = _next(datetime(2026, 11, 10, 9, 0), {"every": "days", "value": 1, "at": "08:00"})
    assert got == datetime(2026, 11, 11, 8, 0)


def test_weekly_day_of_week_roll():
    got = _next(datetime(2026, 11, 11, 9, 0), {"cron": {"hour": 8, "minute": 0, "day_of_week": "mon,wed,sat"}})
    assert got == datetime(2026, 11, 14, 8, 0)


def test_weekly_same_day_before_time():
    got = _next(datetime(2026, 11, 9, 7, 0), {"cron": {"hour": 8, "minute": 0, "day_of_week": "mon"}})
    assert got == datetime(2026, 11, 9, 8, 0)
