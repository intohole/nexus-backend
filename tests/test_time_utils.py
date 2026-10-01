"""TimeUtils 时间工具回归。"""
from datetime import datetime, timedelta, timezone

from nexus.utils import TimeUtils


def test_now_uses_china_tz():
    assert TimeUtils.now().utcoffset() == timedelta(hours=8)


def test_now_utc_is_aware_utc():
    value = TimeUtils.now_utc()
    assert value.tzinfo is timezone.utc
    assert abs((value - datetime.now(timezone.utc)).total_seconds()) < 5


def test_now_naive_is_china_local_without_tz():
    value = TimeUtils.now_naive()
    assert value.tzinfo is None
    assert abs((value - TimeUtils.now().replace(tzinfo=None)).total_seconds()) < 1


def test_now_utc_naive_strips_utc_offset():
    value = TimeUtils.now_utc_naive()
    assert value.tzinfo is None
    assert abs((value - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 5
