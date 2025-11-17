"""任务管理 API

提供任务状态查询、取消等功能
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.common import PaginatedResponse, SuccessResponse, TaskStatusResponse
from sag.api.services.pipeline_service import PipelineService

router = APIRouter()


# Request/Response Models
class BatchDeleteRequest(BaseModel):
    """批量删除请求"""

    task_ids: Optional[List[str]] = None
    status_filter: Optional[List[str]] = None


class TaskStatsResponse(BaseModel):
    """任务统计响应"""

    total: int
    by_status: dict
    by_type: dict


@router.get("/tasks/stats", response_model=SuccessResponse[TaskStatsResponse])
async def get_tasks_stats(
    db: AsyncSession = Depends(get_db),
):
    """
    获取任务统计信息

    **返回**：
    - total: 总任务数
    - by_status: 按状态分组的统计
    - by_type: 按类型分组的统计
    """
    service = PipelineService(db)
    stats = await service.get_tasks_stats()

    return SuccessResponse(
        data=TaskStatsResponse(**stats),
        message="获取统计信息成功",
    )


@router.get("/tasks/{task_id}", response_model=SuccessResponse[TaskStatusResponse])
async def get_task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    查询任务状态

    **返回**：
    - task_id: 任务ID
    - status: pending, running, completed, failed
    - progress: 进度（0.0 - 1.0）
    - result: 结果（如果已完成）
    - error: 错误信息（如果失败）
    """
    service = PipelineService(db)
    task_status = await service.get_task_status(task_id)

    if not task_status:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"任务不存在: {task_id}",
        )

    return SuccessResponse(data=task_status)


@router.get("/tasks", response_model=PaginatedResponse[TaskStatusResponse])
async def list_tasks(
    source_config_id: Optional[str] = Query(None, description="信息源ID筛选"),
    status_param: Optional[str] = Query(None, alias="status", description="状态筛选"),
    search: Optional[str] = Query(None, description="搜索任务ID或消息"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    获取任务列表

    **功能**：
    - 查询所有任务
    - 支持按信息源和状态筛选
    - 支持按任务ID或消息搜索
    """
    service = PipelineService(db)
    tasks, total = await service.list_tasks(
        source_config_id=source_config_id,
        status_filter=status_param,
        search_query=search,
        page=page,
        page_size=page_size,
    )
    return PaginatedResponse.create(
        data=tasks,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/tasks/{task_id}/cancel", response_model=SuccessResponse[dict])
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    取消任务

    **注意**：
    - 只能取消 pending 或 running 状态的任务
    - 已完成或失败的任务无法取消
    """
    service = PipelineService(db)
    success = await service.cancel_task(task_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="任务无法取消（可能已完成或不存在）",
        )

    return SuccessResponse(
        data={"task_id": task_id},
        message="任务已取消",
    )


@router.delete("/tasks/batch", response_model=SuccessResponse[dict])
async def batch_delete_tasks(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    批量删除任务

    **参数**：
    - task_ids: 要删除的任务ID列表（可选）
    - status_filter: 按状态过滤删除（可选），如 ["completed", "failed"]

    **注意**：
    - 至少需要提供 task_ids 或 status_filter 之一
    - 如果同时提供，将取交集
    """
    if not request.task_ids and not request.status_filter:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请提供 task_ids 或 status_filter",
        )

    service = PipelineService(db)
    deleted_count = await service.batch_delete_tasks(
        task_ids=request.task_ids,
        status_filter=request.status_filter,
    )

    return SuccessResponse(
        data={"deleted_count": deleted_count},
        message=f"已删除 {deleted_count} 个任务",
    )

