"""实体类型服务"""

import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, or_, select, func, union_all, case
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.schemas.entity import EntityTypeResponse
from sag.db.models import EntityType, SourceConfig


class EntityTypeService:
    """实体类型服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_entity_type(
        self,
        source_config_id: str,
        type_code: str,
        name: str,
        description: str,
        weight: float = 1.0,
        similarity_threshold: float = 0.8,
        # 🆕 应用范围参数
        scope: str = 'global',
        article_id: Optional[str] = None,
        extraction_prompt: Optional[str] = None,
        extraction_examples: Optional[List[Dict[str, str]]] = None,
        validation_rule: Optional[Dict[str, Any]] = None,
        metadata_schema: Optional[Dict[str, Any]] = None,
        # 🆕 值类型化配置参数
        value_format: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> EntityTypeResponse:
        """创建自定义实体类型"""
        extra_data = {}
        if extraction_prompt:
            extra_data["extraction_prompt"] = extraction_prompt
        if extraction_examples:
            extra_data["extraction_examples"] = extraction_examples
        if validation_rule:
            extra_data["validation_rule"] = validation_rule
        if metadata_schema:
            extra_data["metadata_schema"] = metadata_schema

        entity_type = EntityType(
            id=str(uuid.uuid4()),
            scope=scope,  # 🆕 应用范围
            source_config_id=source_config_id,
            article_id=article_id,  # 🆕 文档ID
            type=type_code,
            name=name,
            description=description,
            weight=Decimal(str(weight)),
            similarity_threshold=Decimal(str(similarity_threshold)),
            is_default=False,
            is_active=True,
            # 🆕 值类型化配置字段
            value_format=value_format,
            value_constraints=value_constraints,
            extra_data=extra_data if extra_data else None,
        )

        self.db.add(entity_type)
        await self.db.commit()
        await self.db.refresh(entity_type)

        return EntityTypeResponse.model_validate(entity_type)

    async def list_entity_types(
        self,
        source_config_id: str,
        page: int = 1,
        page_size: int = 20,
        include_defaults: bool = True,
        only_active: bool = True,
    ) -> Tuple[List[EntityTypeResponse], int]:
        """获取实体类型列表"""
        # 构建查询条件
        conditions = []

        if include_defaults:
            # 包含默认类型（source_config_id为NULL）和该source的自定义类型
            conditions.append(
                or_(
                    EntityType.source_config_id == source_config_id,
                    EntityType.source_config_id.is_(None),
                )
            )
        else:
            # 只查询该source的自定义类型
            conditions.append(EntityType.source_config_id == source_config_id)

        if only_active:
            conditions.append(EntityType.is_active == True)

        query = select(EntityType).where(and_(*conditions))

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(EntityType.is_default.desc(),
                               EntityType.created_time.desc())

        result = await self.db.execute(query)
        entity_types = result.scalars().all()

        return [EntityTypeResponse.model_validate(et) for et in entity_types], total

    async def update_entity_type(
        self,
        entity_type_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        weight: Optional[float] = None,
        similarity_threshold: Optional[float] = None,
        is_active: Optional[bool] = None,
        extraction_prompt: Optional[str] = None,
        extraction_examples: Optional[List[Dict[str, str]]] = None,
        # 🆕 值类型化配置参数
        value_format: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> Optional[EntityTypeResponse]:
        """更新实体类型"""
        result = await self.db.execute(
            select(EntityType).where(EntityType.id == entity_type_id)
        )
        entity_type = result.scalar_one_or_none()

        if not entity_type or entity_type.is_default:
            # 不能更新默认类型
            return None

        if name is not None:
            entity_type.name = name
        if description is not None:
            entity_type.description = description
        if weight is not None:
            entity_type.weight = Decimal(str(weight))
        if similarity_threshold is not None:
            entity_type.similarity_threshold = Decimal(
                str(similarity_threshold))
        if is_active is not None:
            entity_type.is_active = is_active

        # 🆕 更新值类型化配置
        if value_format is not None:
            entity_type.value_format = value_format
        if value_constraints is not None:
            entity_type.value_constraints = value_constraints

        # 更新 extra_data
        if extraction_prompt is not None or extraction_examples is not None:
            extra_data = entity_type.extra_data or {}
            if extraction_prompt is not None:
                extra_data["extraction_prompt"] = extraction_prompt
            if extraction_examples is not None:
                extra_data["extraction_examples"] = extraction_examples
            entity_type.extra_data = extra_data

        await self.db.commit()
        await self.db.refresh(entity_type)

        return EntityTypeResponse.model_validate(entity_type)

    async def delete_entity_type(self, entity_type_id: str) -> bool:
        """删除实体类型"""
        result = await self.db.execute(
            select(EntityType).where(EntityType.id == entity_type_id)
        )
        entity_type = result.scalar_one_or_none()

        if not entity_type or entity_type.is_default:
            # 不能删除默认类型
            return False

        await self.db.delete(entity_type)
        await self.db.commit()

        return True

    async def create_global_entity_type(
        self,
        type_code: str,
        name: str,
        description: str,
        weight: float = 1.0,
        similarity_threshold: float = 0.8,
        extraction_prompt: Optional[str] = None,
        extraction_examples: Optional[List[Dict[str, str]]] = None,
        validation_rule: Optional[Dict[str, Any]] = None,
        metadata_schema: Optional[Dict[str, Any]] = None,
        # 🆕 值类型化配置参数
        value_format: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> EntityTypeResponse:
        """创建全局自定义实体类型（source_config_id 为 NULL）"""
        extra_data = {}
        if extraction_prompt:
            extra_data["extraction_prompt"] = extraction_prompt
        if extraction_examples:
            extra_data["extraction_examples"] = extraction_examples
        if validation_rule:
            extra_data["validation_rule"] = validation_rule
        if metadata_schema:
            extra_data["metadata_schema"] = metadata_schema

        entity_type = EntityType(
            id=str(uuid.uuid4()),
            source_config_id=None,  # 全局类型：source_config_id 为 NULL
            type=type_code,
            name=name,
            description=description,
            weight=Decimal(str(weight)),
            similarity_threshold=Decimal(str(similarity_threshold)),
            is_default=False,  # 自定义类型
            is_active=True,
            # 🆕 值类型化配置字段
            value_format=value_format,
            value_constraints=value_constraints,
            extra_data=extra_data if extra_data else None,
        )

        self.db.add(entity_type)
        await self.db.commit()
        await self.db.refresh(entity_type)

        return EntityTypeResponse.model_validate(entity_type)

    async def list_global_entity_types(
        self,
        page: int = 1,
        page_size: int = 20,
        only_active: bool = True,
    ) -> Tuple[List[EntityTypeResponse], int]:
        """获取全局自定义实体类型列表（source_config_id 为 NULL，is_default 为 False）"""
        # 构建查询条件
        conditions = [
            EntityType.source_config_id.is_(None),  # 全局类型
            EntityType.is_default == False,  # 排除系统默认类型
        ]

        if only_active:
            conditions.append(EntityType.is_active == True)

        query = select(EntityType).where(and_(*conditions))

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(EntityType.created_time.desc())

        result = await self.db.execute(query)
        entity_types = result.scalars().all()

        return [EntityTypeResponse.model_validate(et) for et in entity_types], total

    async def list_all_entity_types(
        self,
        page: int = 1,
        page_size: int = 1000,
        only_active: bool = False,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """获取所有实体类型（系统默认 + 全局自定义 + 所有信息源专属）

        返回格式与其他 list 方法不同，直接返回字典列表包含 _sourceName 字段
        用于前端"全部属性"视图一次性加载
        """
        # 查询所有 entity_types 并 LEFT JOIN source 表获取 source_name
        query = (
            select(
                EntityType,
                SourceConfig.name.label("source_name")
            )
            .outerjoin(SourceConfig, EntityType.source_config_id == SourceConfig.id)
        )

        # 可选：仅返回激活的类型
        if only_active:
            query = query.where(EntityType.is_active == True)

        # 排序：默认类型优先，然后按创建时间倒序
        # MySQL 不支持 NULLS FIRST，使用 CASE WHEN 实现 NULL 值排前面
        query = query.order_by(
            EntityType.is_default.desc(),
            case((EntityType.source_config_id.is_(None), 0),
                 else_=1).asc(),  # NULL 排前面
            EntityType.source_config_id.asc(),  # 非 NULL 值按 source_config_id 排序
            EntityType.created_time.desc()
        )

        # 获取总数（先计数再分页）
        count_query = select(func.count()).select_from(EntityType)
        if only_active:
            count_query = count_query.where(EntityType.is_active == True)

        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页
        query = query.offset((page - 1) * page_size).limit(page_size)

        # 执行查询
        result = await self.db.execute(query)
        rows = result.all()

        # 构建返回数据（包含 _sourceName 字段）
        entity_types_with_source = []
        for entity_type, source_name in rows:
            et_dict = EntityTypeResponse.model_validate(
                entity_type).model_dump()
            if source_name:
                et_dict["_sourceName"] = source_name
            entity_types_with_source.append(et_dict)

        return entity_types_with_source, total

    # 🆕 ============ 文档级别实体类型管理 ============

    async def create_article_entity_type(
        self,
        article_id: str,
        type_code: str,
        name: str,
        description: str,
        weight: float = 1.0,
        similarity_threshold: float = 0.8,
        extraction_prompt: Optional[str] = None,
        extraction_examples: Optional[List[Dict[str, str]]] = None,
        validation_rule: Optional[Dict[str, Any]] = None,
        metadata_schema: Optional[Dict[str, Any]] = None,
        value_format: Optional[str] = None,
        value_constraints: Optional[Dict[str, Any]] = None,
    ) -> EntityTypeResponse:
        """创建文档级别的实体类型"""
        # 先查询文档，获取 source_config_id
        from sag.db.models import Article
        result = await self.db.execute(
            select(Article).where(Article.id == article_id)
        )
        article = result.scalar_one_or_none()
        if not article:
            raise ValueError(f"文档不存在: {article_id}")

        extra_data = {}
        if extraction_prompt:
            extra_data["extraction_prompt"] = extraction_prompt
        if extraction_examples:
            extra_data["extraction_examples"] = extraction_examples
        if validation_rule:
            extra_data["validation_rule"] = validation_rule
        if metadata_schema:
            extra_data["metadata_schema"] = metadata_schema

        entity_type = EntityType(
            id=str(uuid.uuid4()),
            scope='article',  # 🆕 文档级别
            source_config_id=article.source_config_id,  # 继承文档的 source_config_id
            article_id=article_id,  # 🆕 关联文档
            type=type_code,
            name=name,
            description=description,
            weight=Decimal(str(weight)),
            similarity_threshold=Decimal(str(similarity_threshold)),
            is_default=False,
            is_active=True,
            value_format=value_format,
            value_constraints=value_constraints,
            extra_data=extra_data if extra_data else None,
        )

        self.db.add(entity_type)
        await self.db.commit()
        await self.db.refresh(entity_type)

        return EntityTypeResponse.model_validate(entity_type)

    async def list_article_entity_types(
        self,
        article_id: str,
        page: int = 1,
        page_size: int = 20,
        only_active: bool = True,
    ) -> Tuple[List[EntityTypeResponse], int]:
        """获取文档级别的实体类型列表"""
        conditions = [
            EntityType.scope == 'article',
            EntityType.article_id == article_id,
        ]

        if only_active:
            conditions.append(EntityType.is_active == True)

        query = select(EntityType).where(and_(*conditions))

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(EntityType.created_time.desc())

        result = await self.db.execute(query)
        entity_types = result.scalars().all()

        return [EntityTypeResponse.model_validate(et) for et in entity_types], total
