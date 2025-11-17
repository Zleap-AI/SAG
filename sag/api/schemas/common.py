"""通用 API Schema"""

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

# 泛型类型
T = TypeVar("T")


class SuccessResponse(BaseModel, Generic[T]):
    """成功响应"""

    success: bool = Field(default=True, description="是否成功")
    data: T = Field(..., description="响应数据")
    message: Optional[str] = Field(default=None, description="响应消息")


class ErrorResponse(BaseModel):
    """错误响应"""

    success: bool = Field(default=False, description="是否成功")
    error: dict = Field(..., description="错误信息")


class PaginationParams(BaseModel):
    """分页参数"""

    page: int = Field(default=1, ge=1, description="页码")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量")


class PaginatedResponse(BaseModel, Generic[T]):
    """分页响应"""

    success: bool = Field(default=True)
    data: list[T] = Field(..., description="数据列表")
    pagination: dict = Field(..., description="分页信息")

    @classmethod
    def create(
        cls,
        data: list[T],
        total: int,
        page: int,
        page_size: int,
    ) -> "PaginatedResponse[T]":
        """创建分页响应"""
        return cls(
            data=data,
            pagination={
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
            },
        )


class TaskStatusResponse(BaseModel):
    """任务状态响应"""

    task_id: str
    task_type: Optional[str] = None  # document_upload, pipeline_run
    status: str  # pending, processing, completed, failed
    progress: Optional[float] = None  # 0.0 - 100.0
    message: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_time: Optional[str] = None  # ISO格式时间
    updated_time: Optional[str] = None  # ISO格式时间

    # 关联信息
    source_config_id: Optional[str] = None
    source_name: Optional[str] = None
    article_id: Optional[str] = None
    article_title: Optional[str] = None
