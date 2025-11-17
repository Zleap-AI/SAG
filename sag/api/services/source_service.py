"""信息源服务"""

import uuid
from typing import List, Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.schemas.source import SourceConfigResponse
from sag.db.models import SourceConfig, Article, EntityType


class SourceService:
    """信息源服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_source(
        self,
        name: str,
        description: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> SourceConfigResponse:
        """创建信息源"""
        source = SourceConfig(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            config=config or {},
        )

        self.db.add(source)
        await self.db.commit()
        await self.db.refresh(source)

        return SourceConfigResponse.model_validate(source)

    async def get_source(self, source_config_id: str) -> Optional[SourceConfigResponse]:
        """获取信息源"""
        result = await self.db.execute(
            select(SourceConfig).where(SourceConfig.id == source_config_id)
        )
        source = result.scalar_one_or_none()

        if source:
            return SourceConfigResponse.model_validate(source)
        return None

    async def list_sources(
        self,
        page: int = 1,
        page_size: int = 20,
        name_filter: Optional[str] = None,
    ) -> Tuple[List[SourceConfigResponse], int]:
        """获取信息源列表（包含文档数量和实体类型数量）"""
        # 子查询：统计每个 source 的文档数量（所有状态）
        document_count_subq = (
            select(
                Article.source_config_id,
                func.count(Article.id).label("document_count")
            )
            .group_by(Article.source_config_id)
            .subquery()
        )

        # 子查询：统计每个 source 的专属实体类型数量（排除全局类型）
        entity_types_count_subq = (
            select(
                EntityType.source_config_id,
                func.count(EntityType.id).label("entity_types_count")
            )
            .where(EntityType.source_config_id.isnot(None))  # 只统计专属类型，排除 source_config_id 为 NULL 的全局类型
            .group_by(EntityType.source_config_id)
            .subquery()
        )

        # 主查询：获取 source 信息并 JOIN 两个统计子查询
        query = (
            select(
                SourceConfig,
                func.coalesce(document_count_subq.c.document_count, 0).label("document_count"),
                func.coalesce(entity_types_count_subq.c.entity_types_count, 0).label("entity_types_count")
            )
            .outerjoin(document_count_subq, SourceConfig.id == document_count_subq.c.source_config_id)
            .outerjoin(entity_types_count_subq, SourceConfig.id == entity_types_count_subq.c.source_config_id)
        )

        if name_filter:
            query = query.where(SourceConfig.name.like(f"%{name_filter}%"))

        # 获取总数（source 的数量）
        count_query = select(func.count()).select_from(SourceConfig)
        if name_filter:
            count_query = count_query.where(SourceConfig.name.like(f"%{name_filter}%"))

        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页和排序
        query = query.order_by(SourceConfig.created_time.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await self.db.execute(query)
        rows = result.all()

        # 构建响应
        sources = []
        for source, document_count, entity_types_count in rows:
            source_dict = {
                "id": source.id,
                "name": source.name,
                "description": source.description,
                "config": source.config,
                "created_time": source.created_time,
                "updated_time": source.updated_time,
                "document_count": document_count,
                "entity_types_count": entity_types_count,
            }
            sources.append(SourceConfigResponse.model_validate(source_dict))

        return sources, total

    async def update_source(
        self,
        source_config_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        config: Optional[dict] = None,
    ) -> Optional[SourceConfigResponse]:
        """更新信息源"""
        result = await self.db.execute(
            select(SourceConfig).where(SourceConfig.id == source_config_id)
        )
        source = result.scalar_one_or_none()

        if not source:
            return None

        if name is not None:
            source.name = name
        if description is not None:
            source.description = description
        if config is not None:
            source.config = config

        await self.db.commit()
        await self.db.refresh(source)

        return SourceConfigResponse.model_validate(source)

    async def delete_source(self, source_config_id: str) -> bool:
        """删除信息源"""
        result = await self.db.execute(
            select(SourceConfig).where(SourceConfig.id == source_config_id)
        )
        source = result.scalar_one_or_none()

        if not source:
            return False

        await self.db.delete(source)
        await self.db.commit()

        return True

