"""
Base schemas and mixins for API responses

提供通用的基类和 Mixin，用于统一处理数据序列化
"""

from datetime import datetime, timezone
from pydantic import BaseModel, field_serializer


class TimestampMixin(BaseModel):
    """
    时间戳序列化 Mixin

    确保所有 datetime 字段返回标准 ISO 8601 格式（UTC时区，带 Z 后缀）

    Example:
        数据库: 2025-10-28 06:57:17 (UTC, 无时区标记)
        API返回: "2025-10-28T06:57:17Z" (ISO 8601 + UTC标记)
        前端显示: 2025/10/28 14:57 (浏览器本地时区，如+08:00)
    """

    @field_serializer('created_time', 'updated_time', when_used='always', check_fields=False)
    def serialize_datetime(self, dt: datetime | None) -> str | None:
        """
        将 datetime 序列化为 ISO 8601 UTC 格式字符串

        Args:
            dt: datetime 对象（可能有或没有时区信息）

        Returns:
            ISO 8601 格式字符串，带 Z 后缀，或 None
        """
        if dt is None:
            return None

        # 如果没有时区信息，假定为 UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # 转换为 ISO 8601 格式，将 +00:00 替换为 Z
        return dt.isoformat().replace('+00:00', 'Z')
