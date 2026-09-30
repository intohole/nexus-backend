"""时间工具：中国时区为中心的 datetime 便捷族。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta


class TimeUtils:
    CHINA_TZ: timezone = timezone(timedelta(hours=8))
    UTC_TZ: timezone = timezone.utc

    @classmethod
    def now(cls) -> datetime:
        return datetime.now(cls.CHINA_TZ)

    @classmethod
    def now_utc(cls) -> datetime:
        return datetime.now(cls.UTC_TZ)

    @classmethod
    def to_cn(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=cls.UTC_TZ)
        return dt.astimezone(cls.CHINA_TZ)

    @classmethod
    def to_utc(cls, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=cls.CHINA_TZ)
        return dt.astimezone(cls.UTC_TZ)

    @classmethod
    def to_iso(cls, dt: datetime) -> str:
        return cls.to_cn(dt).isoformat()

    @classmethod
    def from_iso(cls, iso_str: str) -> datetime:
        return datetime.fromisoformat(iso_str)

    @classmethod
    def format(cls, dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        return cls.to_cn(dt).strftime(fmt)

    @classmethod
    def now_naive(cls) -> datetime:
        return cls.now().replace(tzinfo=None)

    @classmethod
    def ensure_naive(cls, dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt

    @classmethod
    def parse(cls, dt_str: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
        dt = datetime.strptime(dt_str, fmt)
        return dt.replace(tzinfo=cls.CHINA_TZ)
