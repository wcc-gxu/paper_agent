"""时区工具：所有用户面时间戳强制北京时间（DB 仍存 UTC）。"""

from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间 datetime（带时区）。"""
    return datetime.now(BEIJING_TZ)


def beijing_now_iso() -> str:
    """返回当前北京时间 ISO 8601 字符串（毫秒精度）。"""
    return beijing_now().isoformat(timespec="milliseconds")


def utc_to_beijing(utc_str: str) -> str:
    """将 UTC ISO 字符串转为北京时间字符串。"""
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    return dt.astimezone(BEIJING_TZ).isoformat(timespec="milliseconds")
