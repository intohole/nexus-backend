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


def test_ensure_naive_utc_converts_aware_keeps_naive():
    aware_cn = datetime(2026, 10, 8, 12, 0, tzinfo=TimeUtils.CHINA_TZ)
    assert TimeUtils.ensure_naive_utc(aware_cn) == datetime(2026, 10, 8, 4, 0)
    naive = datetime(2026, 10, 8, 12, 0)
    assert TimeUtils.ensure_naive_utc(naive) is naive


def test_ensure_naive_cn_converts_aware_keeps_naive():
    aware_utc = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
    assert TimeUtils.ensure_naive_cn(aware_utc) == datetime(2026, 10, 8, 12, 0)
    naive = datetime(2026, 10, 8, 12, 0)
    assert TimeUtils.ensure_naive_cn(naive) is naive


def test_assume_tags_only_when_naive():
    naive = datetime(2026, 1, 1, 8, 0)
    assert TimeUtils.assume_cn(naive).utcoffset() == timedelta(hours=8)
    aware = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert TimeUtils.assume_cn(aware) is aware
    assert TimeUtils.assume_utc(naive).utcoffset() == timedelta(0)


def test_fmt_cn_treats_naive_as_cn_wall_clock():
    naive_wall = datetime(2026, 10, 8, 23, 30)
    assert TimeUtils.fmt_cn(naive_wall, "%H:%M") == "23:30"
    aware_utc = datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)
    assert TimeUtils.fmt_cn(aware_utc, "%H:%M") == "23:30"
