"""实体维度管理 API

提供自定义实体类型的管理功能
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.common import PaginatedResponse, SuccessResponse
from sag.api.schemas.entity import (
    EntityTypeCreateRequest,
    EntityTypeResponse,
    EntityTypeUpdateRequest,
)
from sag.api.services.entity_service import EntityTypeService

router = APIRouter()


@router.post(
    "/sources/{source_config_id}/entity-types",
    response_model=SuccessResponse[EntityTypeResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_entity_type(
    source_config_id: str,
    request: EntityTypeCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    创建自定义实体类型

    **功能**：
    - 为指定信息源创建自定义实体维度
    - 自动注册到系统中，Extract 时自动识别

    **参数**：
    - source_config_id: 信息源ID
    - type: 类型标识符（如 "project_stage"）
    - name: 类型名称（如 "项目阶段"）
    - description: 描述（用于指导LLM提取）
    - weight: 权重（0.0-9.99，默认1.0）
    - similarity_threshold: 相似度阈值（0.0-1.0，默认0.8）
    - extraction_prompt: 自定义提取提示词
    - extraction_examples: Few-shot 示例

    **示例**：
    ```json
    {
      "type": "project_stage",
      "name": "项目阶段",
      "description": "项目的生命周期阶段（需求分析、设计、开发、测试、上线）",
      "weight": 1.2,
      "similarity_threshold": 0.85,
      "extraction_examples": [
        {"input": "当前处于需求分析阶段", "output": "需求分析阶段"}
      ]
    }
    ```
    """
    service = EntityTypeService(db)
    entity_type = await service.create_entity_type(
        source_config_id=source_config_id,
        type_code=request.type,
        name=request.name,
        description=request.description,
        weight=request.weight,
        similarity_threshold=request.similarity_threshold,
        # 🆕 应用范围参数
        scope=request.scope,
        article_id=request.article_id,
        extraction_prompt=request.extraction_prompt,
        extraction_examples=request.extraction_examples,
        validation_rule=request.validation_rule,
        metadata_schema=request.metadata_schema,
        # 🆕 传递值类型化配置参数
        value_format=request.value_format,
        value_constraints=request.value_constraints,
    )
    return SuccessResponse(data=entity_type, message="实体类型创建成功")


@router.get(
    "/sources/{source_config_id}/entity-types", response_model=PaginatedResponse[EntityTypeResponse]
)
async def list_entity_types(
    source_config_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    include_defaults: bool = Query(True, description="是否包含默认类型"),
    only_active: bool = Query(True, description="只显示启用的类型"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取实体类型列表

    **功能**：
    - 查询信息源的自定义实体类型
    - 可选包含系统默认类型

    **参数**：
    - source_config_id: 信息源ID
    - include_defaults: 是否包含默认类型（time, location, person 等）
    - only_active: 只显示启用的类型
    """
    service = EntityTypeService(db)
    entity_types, total = await service.list_entity_types(
        source_config_id=source_config_id,
        page=page,
        page_size=page_size,
        include_defaults=include_defaults,
        only_active=only_active,
    )
    return PaginatedResponse.create(
        data=entity_types,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/entity-types/defaults", response_model=SuccessResponse[List[EntityTypeResponse]])
async def get_default_entity_types():
    """
    获取系统默认实体类型

    **返回顺序**（固定顺序）：
    - time: 时间（权重1.0，阈值0.900）
    - location: 地点（权重1.0，阈值0.750）
    - person: 人员（权重1.0，阈值0.950）
    - action: 行为（权重1.5，阈值0.800）
    - topic: 话题（权重1.8，阈值0.600）
    - tags: 标签（权重0.5，阈值0.700）
    """
    from sag.models.entity import DEFAULT_ENTITY_TYPES

    return SuccessResponse(
        data=[EntityTypeResponse.model_validate(
            et) for et in DEFAULT_ENTITY_TYPES]
    )


@router.get("/entity-types/all", response_model=PaginatedResponse[dict])
async def list_all_entity_types(
    page: int = Query(1, ge=1),
    page_size: int = Query(1000, ge=1, le=2000),
    only_active: bool = Query(False, description="只显示启用的类型"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取所有实体类型（系统默认 + 全局自定义 + 所有信息源专属）

    **功能**：
    - 一次性获取所有实体类型，用于前端"全部属性"视图
    - 包含系统默认类型（is_default=True, source_config_id=NULL）
    - 包含全局自定义类型（is_default=False, source_config_id=NULL）
    - 包含所有信息源专属类型（source_config_id!=NULL），并附带 _sourceName 字段

    **参数**：
    - page: 页码（从1开始）
    - page_size: 每页数量（1-2000，默认1000）
    - only_active: 只显示启用的类型

    **返回**：
    - data: 实体类型列表，信息源专属类型会包含 _sourceName 字段
    """
    service = EntityTypeService(db)
    entity_types, total = await service.list_all_entity_types(
        page=page,
        page_size=page_size,
        only_active=only_active,
    )
    return PaginatedResponse.create(
        data=entity_types,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/entity-types/{entity_type_id}",
    response_model=SuccessResponse[EntityTypeResponse],
)
async def update_entity_type(
    entity_type_id: str,
    request: EntityTypeUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    更新实体类型

    **注意**：
    - 只能更新自定义类型，不能修改系统默认类型
    - 更新后会影响后续的 Extract 操作
    """
    service = EntityTypeService(db)
    entity_type = await service.update_entity_type(
        entity_type_id=entity_type_id,
        name=request.name,
        description=request.description,
        weight=request.weight,
        similarity_threshold=request.similarity_threshold,
        is_active=request.is_active,
        extraction_prompt=request.extraction_prompt,
        extraction_examples=request.extraction_examples,
        # 🆕 传递值类型化配置参数
        value_format=request.value_format,
        value_constraints=request.value_constraints,
    )
    if not entity_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体类型不存在或无法修改: {entity_type_id}",
        )
    return SuccessResponse(data=entity_type, message="实体类型更新成功")


@router.delete("/entity-types/{entity_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity_type(
    entity_type_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    删除实体类型

    **注意**：
    - 只能删除自定义类型
    - 删除后，该类型的实体会被标记为孤立状态
    """
    service = EntityTypeService(db)
    success = await service.delete_entity_type(entity_type_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"实体类型不存在或无法删除: {entity_type_id}",
        )


@router.post(
    "/entity-types",
    response_model=SuccessResponse[EntityTypeResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_global_entity_type(
    request: EntityTypeCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    创建全局自定义实体类型（不绑定信息源）

    **功能**：
    - 创建全局通用的自定义实体维度
    - source_config_id 为 NULL，is_default 为 False
    - 所有信息源都可以使用这些全局自定义类型

    **参数**：
    - type: 类型标识符（如 "company"）
    - name: 类型名称（如 "公司"）
    - description: 描述（用于指导LLM提取）
    - weight: 权重（0.0-9.99，默认1.0）
    - similarity_threshold: 相似度阈值（0.0-1.0，默认0.8）
    - extraction_prompt: 自定义提取提示词
    - extraction_examples: Few-shot 示例

    **示例**：
    ```json
    {
      "type": "company",
      "name": "公司",
      "description": "公司、企业、组织名称",
      "weight": 1.3,
      "similarity_threshold": 0.85
    }
    ```
    """
    service = EntityTypeService(db)
    entity_type = await service.create_global_entity_type(
        type_code=request.type,
        name=request.name,
        description=request.description,
        weight=request.weight,
        similarity_threshold=request.similarity_threshold,
        extraction_prompt=request.extraction_prompt,
        extraction_examples=request.extraction_examples,
        validation_rule=request.validation_rule,
        metadata_schema=request.metadata_schema,
        # 🆕 传递值类型化配置参数
        value_format=request.value_format,
        value_constraints=request.value_constraints,
    )
    return SuccessResponse(data=entity_type, message="全局实体类型创建成功")


@router.get("/entity-types", response_model=PaginatedResponse[EntityTypeResponse])
async def list_global_entity_types(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_active: bool = Query(True, description="只显示启用的类型"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取全局自定义实体类型列表

    **功能**：
    - 查询所有全局自定义实体类型（source_config_id 为 NULL，is_default 为 False）
    - 不包含系统默认类型（使用 /entity-types/defaults 获取）
    - 不包含信息源专属类型（使用 /sources/{source_config_id}/entity-types 获取）

    **参数**：
    - page: 页码（从1开始）
    - page_size: 每页数量（1-100）
    - only_active: 只显示启用的类型
    """
    service = EntityTypeService(db)
    entity_types, total = await service.list_global_entity_types(
        page=page,
        page_size=page_size,
        only_active=only_active,
    )
    return PaginatedResponse.create(
        data=entity_types,
        total=total,
        page=page,
        page_size=page_size,
    )


# 🆕 ============ 文档级别实体类型管理 ============

@router.post(
    "/documents/{article_id}/entity-types",
    response_model=SuccessResponse[EntityTypeResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_article_entity_type(
    article_id: str,
    request: EntityTypeCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    创建文档级别的实体类型

    **功能**：
    - 为指定文档创建专属的实体维度
    - scope='article'，仅该文档提取时使用
    - 优先级最高，可覆盖信息源和全局属性

    **参数**：
    - article_id: 文档ID
    - 其他参数同信息源级别
    """
    service = EntityTypeService(db)
    entity_type = await service.create_article_entity_type(
        article_id=article_id,
        type_code=request.type,
        name=request.name,
        description=request.description,
        weight=request.weight,
        similarity_threshold=request.similarity_threshold,
        extraction_prompt=request.extraction_prompt,
        extraction_examples=request.extraction_examples,
        validation_rule=request.validation_rule,
        metadata_schema=request.metadata_schema,
        value_format=request.value_format,
        value_constraints=request.value_constraints,
    )
    return SuccessResponse(data=entity_type, message="文档级别实体类型创建成功")


@router.get(
    "/documents/{article_id}/entity-types",
    response_model=PaginatedResponse[EntityTypeResponse]
)
async def list_article_entity_types(
    article_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_active: bool = Query(True, description="只显示启用的类型"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取文档级别的实体类型列表

    **功能**：
    - 查询指定文档的专属实体类型
    - scope='article'，article_id=指定值
    """
    service = EntityTypeService(db)
    entity_types, total = await service.list_article_entity_types(
        article_id=article_id,
        page=page,
        page_size=page_size,
        only_active=only_active,
    )
    return PaginatedResponse.create(
        data=entity_types,
        total=total,
        page=page,
        page_size=page_size,
    )
