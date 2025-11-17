"""信息源管理 API

提供信息源的 CRUD 操作
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.common import PaginatedResponse, SuccessResponse
from sag.api.schemas.source import (
    SourceConfigCreateRequest,
    SourceConfigResponse,
    SourceConfigUpdateRequest,
)
from sag.api.services.source_service import SourceService

router = APIRouter()


@router.post(
    "/sources",
    response_model=SuccessResponse[SourceConfigResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    request: SourceConfigCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    创建信息源

    **功能**：
    - 创建新的信息源
    - 自动生成唯一ID
    - 初始化默认配置

    **参数**：
    - name: 信息源名称（必填）
    - description: 描述信息
    - config: 偏好设置，如 {"focus": ["AI"], "language": "zh"}
    """
    service = SourceService(db)
    source = await service.create_source(
        name=request.name,
        description=request.description,
        config=request.config,
    )
    return SuccessResponse(data=source, message="信息源创建成功")


@router.get("/sources", response_model=PaginatedResponse[SourceConfigResponse])
async def list_sources(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    name: Optional[str] = Query(None, description="名称筛选"),
    db: AsyncSession = Depends(get_db),
):
    """
    获取信息源列表

    **功能**：
    - 分页查询信息源
    - 支持名称模糊搜索
    """
    service = SourceService(db)
    sources, total = await service.list_sources(
        page=page,
        page_size=page_size,
        name_filter=name,
    )
    return PaginatedResponse.create(
        data=sources,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/sources/{source_config_id}", response_model=SuccessResponse[SourceConfigResponse])
async def get_source(
    source_config_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    获取信息源详情

    **参数**：
    - source_config_id: 信息源ID
    """
    service = SourceService(db)
    source = await service.get_source(source_config_id)
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"信息源不存在: {source_config_id}",
        )
    return SuccessResponse(data=source)


@router.patch("/sources/{source_config_id}", response_model=SuccessResponse[SourceConfigResponse])
async def update_source(
    source_config_id: str,
    request: SourceConfigUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    更新信息源

    **参数**：
    - source_config_id: 信息源ID
    - name: 新名称（可选）
    - description: 新描述（可选）
    - config: 新配置（可选）
    """
    service = SourceService(db)
    source = await service.update_source(
        source_config_id=source_config_id,
        name=request.name,
        description=request.description,
        config=request.config,
    )
    if not source:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"信息源不存在: {source_config_id}",
        )
    return SuccessResponse(data=source, message="信息源更新成功")


@router.delete("/sources/{source_config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_config_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    删除信息源

    **注意**：
    - 会级联删除该信息源下的所有数据
    - 包括文章、事项、实体等
    - 此操作不可恢复

    **参数**：
    - source_config_id: 信息源ID
    """
    service = SourceService(db)
    success = await service.delete_source(source_config_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"信息源不存在: {source_config_id}",
        )

