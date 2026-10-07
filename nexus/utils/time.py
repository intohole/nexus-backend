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
    def now_utc_naive(cls) -> datetime:
        return cls.now_utc().replace(tzinfo=None)

    @classmethod
    def ensure_naive(cls, dt: datetime) -> datetime:
        """只剥 tzinfo 不做时区换算——调用方必须自证 naive 基准正确。"""
        if dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt

    @classmethod
    def ensure_naive_utc(cls, dt: datetime) -> datetime:
        """aware 先折算 UTC 再剥 tz；naive 原样。utcnow() 旧惯例的安全替换。"""
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(cls.UTC_TZ).replace(tzinfo=None)

    @classmethod
    def ensure_naive_cn(cls, dt: datetime) -> datetime:
        """aware 先折算中国墙钟再剥 tz；naive 原样。CN 墙钟 naive 列的统一入口。"""
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(cls.CHINA_TZ).replace(tzinfo=None)

    @classmethod
    def assume_utc(cls, dt: datetime) -> datetime:
        """naive 打 UTC 标（幂等：aware 原样）。"""
        return dt.replace(tzinfo=cls.UTC_TZ) if dt.tzinfo is None else dt

    @classmethod
    def assume_cn(cls, dt: datetime) -> datetime:
        """naive 打中国时区标（幂等：aware 原样）。"""
        return dt.replace(tzinfo=cls.CHINA_TZ) if dt.tzinfo is None else dt

    @classmethod
    def fmt_cn(cls, dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """展示格式化：naive 视为已是 CN 墙钟不换算（与 format 的 naive=UTC 假定区分）。"""
        return cls.ensure_naive_cn(dt).strftime(fmt)

    @classmethod
    def parse(cls, dt_str: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
        dt = datetime.strptime(dt_str, fmt)
        return dt.replace(tzinfo=cls.CHINA_TZ)
