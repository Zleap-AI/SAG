"""
时间处理工具模块
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


def get_utc_now() -> datetime:
    """
    获取当前UTC时间

    Returns:
        UTC datetime对象
    """
    return datetime.now(timezone.utc)


def parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    """
    解析ISO 8601格式时间字符串

    Args:
        dt_str: ISO格式时间字符串

    Returns:
        datetime对象，解析失败返回None
    """
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def format_datetime(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    格式化datetime对象

    Args:
        dt: datetime对象
        fmt: 格式字符串

    Returns:
        格式化后的字符串
    """
    return dt.strftime(fmt)


def get_time_ago(dt: datetime) -> str:
    """
    获取相对时间描述

    Args:
        dt: datetime对象

    Returns:
        相对时间描述（如"3分钟前"）
    """
    now = get_utc_now()

    # 确保dt有时区信息
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt

    if delta < timedelta(minutes=1):
        return "刚刚"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"{minutes}分钟前"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"{hours}小时前"
    elif delta < timedelta(days=30):
        days = delta.days
        return f"{days}天前"
    elif delta < timedelta(days=365):
        months = int(delta.days / 30)
        return f"{months}个月前"
    else:
        years = int(delta.days / 365)
        return f"{years}年前"


def calculate_time_decay(
    created_time: datetime,
    decay_factor: float = 0.01,
) -> float:
    """
    计算时间衰减因子

    使用指数衰减公式：e^(-λt)

    Args:
        created_time: 创建时间
        decay_factor: 衰减因子λ（默认0.01）

    Returns:
        衰减因子（0-1之间）
    """
    import math

    now = get_utc_now()

    # 确保created_time有时区信息
    if created_time.tzinfo is None:
        created_time = created_time.replace(tzinfo=timezone.utc)

    days_ago = (now - created_time).days
    return math.exp(-decay_factor * days_ago)
