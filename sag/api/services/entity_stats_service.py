"""
实体统计分析服务

提供实体值的统计分析功能：
- 数值分布统计（价格、金额等）
- 时间趋势分析（事件时间线）
- 分类占比统计（枚举值分布）
- 实体共现关系分析
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional
import logging

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag.db.models import Entity, EntityType, EventEntity, SourceEvent

logger = logging.getLogger(__name__)


class EntityStatsService:
    """实体统计分析服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_numeric_distribution(
        self,
        source_config_id: str,
        entity_type: Optional[str] = None,
        value_type: str = "float",  # "int" or "float"
        bins: int = 10
    ) -> Dict[str, Any]:
        """
        获取数值分布统计

        Args:
            source_config_id: 信息源ID
            entity_type: 实体类型（可选，如 "price"）
            value_type: 值类型（"int" 或 "float"）
            bins: 分箱数量

        Returns:
            {
                "total_count": 100,
                "min": 0.0,
                "max": 10000.0,
                "avg": 1500.5,
                "median": 1200.0,
                "distribution": [
                    {"range": "0-1000", "count": 20},
                    {"range": "1000-2000", "count": 30},
                    ...
                ]
            }
        """
        # 构建查询条件
        conditions = [
            Entity.source_config_id == source_config_id,
            Entity.value_type == value_type
        ]
        if entity_type:
            conditions.append(Entity.type == entity_type)

        # 值字段
        value_field = Entity.int_value if value_type == "int" else Entity.float_value

        # 基础统计
        stmt = select(
            func.count(Entity.id).label("total"),
            func.min(value_field).label("min_val"),
            func.max(value_field).label("max_val"),
            func.avg(value_field).label("avg_val"),
        ).where(and_(*conditions))

        result = await self.db.execute(stmt)
        stats = result.one()

        if not stats.total or stats.total == 0:
            return {
                "total_count": 0,
                "min": None,
                "max": None,
                "avg": None,
                "median": None,
                "distribution": []
            }

        # 分箱统计
        min_val = float(stats.min_val)
        max_val = float(stats.max_val)
        bin_width = (max_val - min_val) / bins if bins > 0 else 0

        distribution = []
        if bin_width > 0:
            for i in range(bins):
                bin_start = min_val + i * bin_width
                bin_end = min_val + (i + 1) * bin_width

                # 统计该区间的实体数
                bin_stmt = select(func.count(Entity.id)).where(
                    and_(
                        *conditions,
                        value_field >= bin_start,
                        value_field < bin_end if i < bins - 1 else value_field <= bin_end
                    )
                )
                bin_result = await self.db.execute(bin_stmt)
                count = bin_result.scalar()

                distribution.append({
                    "range": f"{bin_start:.2f}-{bin_end:.2f}",
                    "count": count or 0
                })

        return {
            "total_count": stats.total,
            "min": float(stats.min_val) if stats.min_val else None,
            "max": float(stats.max_val) if stats.max_val else None,
            "avg": float(stats.avg_val) if stats.avg_val else None,
            "median": None,  # 中位数需要复杂查询，暂不实现
            "distribution": distribution
        }

    async def get_time_trend(
        self,
        source_config_id: str,
        entity_type: Optional[str] = None,
        granularity: str = "day"  # "year", "month", "day"
    ) -> Dict[str, Any]:
        """
        获取时间趋势分析

        Args:
            source_config_id: 信息源ID
            entity_type: 实体类型（可选）
            granularity: 时间粒度（"year", "month", "day"）

        Returns:
            {
                "total_count": 100,
                "timeline": [
                    {"date": "2024-01", "count": 10},
                    {"date": "2024-02", "count": 15},
                    ...
                ]
            }
        """
        # 构建查询条件
        conditions = [
            Entity.source_config_id == source_config_id,
            Entity.value_type == "datetime",
            Entity.datetime_value.isnot(None)
        ]
        if entity_type:
            conditions.append(Entity.type == entity_type)

        # 根据粒度选择日期格式化函数
        if granularity == "year":
            date_format = func.date_format(Entity.datetime_value, '%Y')
        elif granularity == "month":
            date_format = func.date_format(Entity.datetime_value, '%Y-%m')
        else:  # day
            date_format = func.date_format(Entity.datetime_value, '%Y-%m-%d')

        # 按时间分组统计
        stmt = select(
            date_format.label("date"),
            func.count(Entity.id).label("count")
        ).where(
            and_(*conditions)
        ).group_by(
            date_format
        ).order_by(
            date_format
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        timeline = [
            {"date": row.date, "count": row.count}
            for row in rows
        ]

        return {
            "total_count": sum(item["count"] for item in timeline),
            "timeline": timeline
        }

    async def get_enum_distribution(
        self,
        source_config_id: str,
        entity_type: str
    ) -> Dict[str, Any]:
        """
        获取枚举值分布统计

        Args:
            source_config_id: 信息源ID
            entity_type: 实体类型

        Returns:
            {
                "total_count": 100,
                "distribution": [
                    {"value": "A轮融资", "count": 20, "percentage": 0.20},
                    {"value": "B轮融资", "count": 15, "percentage": 0.15},
                    ...
                ]
            }
        """
        # 按枚举值分组统计
        stmt = select(
            Entity.enum_value,
            func.count(Entity.id).label("count")
        ).where(
            and_(
                Entity.source_config_id == source_config_id,
                Entity.type == entity_type,
                Entity.value_type == "enum",
                Entity.enum_value.isnot(None)
            )
        ).group_by(
            Entity.enum_value
        ).order_by(
            func.count(Entity.id).desc()
        )

        result = await self.db.execute(stmt)
        rows = result.all()

        total = sum(row.count for row in rows)

        distribution = [
            {
                "value": row.enum_value,
                "count": row.count,
                "percentage": round(row.count / total, 4) if total > 0 else 0
            }
            for row in rows
        ]

        return {
            "total_count": total,
            "distribution": distribution
        }

    async def get_entity_cooccurrence(
        self,
        source_config_id: str,
        entity_id: str,
        limit: int = 10
    ) -> Dict[str, Any]:
        """
        获取实体共现关系

        查找与指定实体经常一起出现在同一事项中的其他实体

        Args:
            source_config_id: 信息源ID
            entity_id: 目标实体ID
            limit: 返回数量限制

        Returns:
            {
                "entity_id": "xxx",
                "entity_name": "张三",
                "cooccurrence": [
                    {
                        "entity_id": "yyy",
                        "entity_name": "AI技术",
                        "entity_type": "topic",
                        "count": 5,
                        "strength": 0.83
                    },
                    ...
                ]
            }
        """
        # 获取目标实体信息
        entity_stmt = select(Entity).where(Entity.id == entity_id)
        entity_result = await self.db.execute(entity_stmt)
        target_entity = entity_result.scalar_one_or_none()

        if not target_entity:
            return {
                "entity_id": entity_id,
                "entity_name": None,
                "cooccurrence": []
            }

        # 查找包含目标实体的所有事项
        event_ids_stmt = select(EventEntity.event_id).where(
            EventEntity.entity_id == entity_id
        )
        event_ids_result = await self.db.execute(event_ids_stmt)
        event_ids = [row[0] for row in event_ids_result.all()]

        if not event_ids:
            return {
                "entity_id": entity_id,
                "entity_name": target_entity.name,
                "cooccurrence": []
            }

        # 查找这些事项中的其他实体
        cooccur_stmt = select(
            Entity.id,
            Entity.name,
            Entity.type,
            func.count(EventEntity.event_id).label("count")
        ).join(
            EventEntity, EventEntity.entity_id == Entity.id
        ).where(
            and_(
                EventEntity.event_id.in_(event_ids),
                Entity.id != entity_id,
                Entity.source_config_id == source_config_id
            )
        ).group_by(
            Entity.id, Entity.name, Entity.type
        ).order_by(
            func.count(EventEntity.event_id).desc()
        ).limit(limit)

        cooccur_result = await self.db.execute(cooccur_stmt)
        rows = cooccur_result.all()

        # 计算共现强度（简化版：共现次数 / 目标实体出现次数）
        total_events = len(event_ids)

        cooccurrence = [
            {
                "entity_id": row.id,
                "entity_name": row.name,
                "entity_type": row.type,
                "count": row.count,
                "strength": round(row.count / total_events, 2) if total_events > 0 else 0
            }
            for row in rows
        ]

        return {
            "entity_id": entity_id,
            "entity_name": target_entity.name,
            "cooccurrence": cooccurrence
        }

    async def get_entity_summary(
        self,
        source_config_id: str,
        entity_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取实体统计摘要

        Args:
            source_config_id: 信息源ID
            entity_type: 实体类型（可选）

        Returns:
            {
                "total_entities": 1000,
                "by_value_type": {
                    "int": 100,
                    "float": 200,
                    "datetime": 150,
                    "bool": 50,
                    "enum": 100,
                    "text": 400
                },
                "by_entity_type": {
                    "person": 300,
                    "topic": 400,
                    ...
                }
            }
        """
        # 基础条件
        conditions = [Entity.source_config_id == source_config_id]
        if entity_type:
            conditions.append(Entity.type == entity_type)

        # 总实体数
        total_stmt = select(func.count(Entity.id)).where(and_(*conditions))
        total_result = await self.db.execute(total_stmt)
        total = total_result.scalar()

        # 按值类型统计
        value_type_stmt = select(
            Entity.value_type,
            func.count(Entity.id).label("count")
        ).where(
            and_(*conditions)
        ).group_by(
            Entity.value_type
        )
        value_type_result = await self.db.execute(value_type_stmt)
        by_value_type = {
            row.value_type or "text": row.count
            for row in value_type_result.all()
        }

        # 按实体类型统计（仅在未指定entity_type时）
        by_entity_type = {}
        if not entity_type:
            entity_type_stmt = select(
                Entity.type,
                func.count(Entity.id).label("count")
            ).where(
                Entity.source_config_id == source_config_id
            ).group_by(
                Entity.type
            )
            entity_type_result = await self.db.execute(entity_type_stmt)
            by_entity_type = {
                row.type: row.count
                for row in entity_type_result.all()
            }

        return {
            "total_entities": total or 0,
            "by_value_type": by_value_type,
            "by_entity_type": by_entity_type
        }
