"""文档管理 API

提供文档上传、列表查询、删除等功能
"""

from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.deps import get_db
from sag.api.schemas.common import PaginatedResponse, SuccessResponse
from sag.api.schemas.document import DocumentResponse, DocumentUploadResponse, DocumentUpdate, ArticleSectionResponse, SourceEventResponse
from sag.api.services.document_service import DocumentService

router = APIRouter()


@router.post(
    "/sources/{source_config_id}/documents/upload",
    response_model=SuccessResponse[DocumentUploadResponse],
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    source_config_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="文档文件"),
    background: Optional[str] = Form(None, description="背景信息"),
    auto_process: bool = Form(True, description="是否自动 Load+Extract"),
    entity_types: Optional[str] = Form(None, description="文档专属实体类型配置（JSON格式）"),
    db: AsyncSession = Depends(get_db),
):
    """
    上传文档（异步处理）

    **功能**：
    - 上传文档文件（支持 Markdown、PDF、TXT 等）
    - 立即返回文档ID
    - 可选：同时创建文档专属实体类型（快捷设置）
    - 后台自动执行 Load + Extract

    **参数**：
    - source_config_id: 信息源ID
    - file: 文档文件
    - background: 背景信息（补充元数据生成上下文）
    - auto_process: 是否自动处理（Load + Extract）
    - entity_types: 文档专属实体类型配置（JSON数组字符串）

    **返回**：
    - file_path: 文件保存路径
    - article_id: 文章ID（立即返回）
    - message: 状态消息
    """
    service = DocumentService(db)

    # 检查文件类型
    allowed_extensions = {".md", ".txt", ".pdf", ".html"}
    file_ext = Path(file.filename or "unknown").suffix.lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: {file_ext}。支持的类型: {', '.join(allowed_extensions)}",
        )

    # 上传文件（立即返回）
    result = await service.upload_document(
        source_config_id=source_config_id,
        file=file,
        background=background,
        auto_process=auto_process,
    )

    # 🆕 如果提供了实体类型配置，批量创建文档专属实体类型
    if entity_types and result.article_id:
        import json
        from sag.api.services.entity_service import EntityTypeService
        
        try:
            entity_types_data = json.loads(entity_types)
            if isinstance(entity_types_data, list) and len(entity_types_data) > 0:
                entity_service = EntityTypeService(db)
                for et_data in entity_types_data:
                    if et_data.get('type') and et_data.get('name'):
                        await entity_service.create_article_entity_type(
                            article_id=result.article_id,
                            type_code=et_data['type'],
                            name=et_data['name'],
                            description=et_data.get('description', ''),
                            weight=et_data.get('weight', 1.0),
                            similarity_threshold=et_data.get('similarity_threshold', 0.8),
                            value_constraints=et_data.get('value_constraints'),  # 🆕 值类型配置
                        )
        except json.JSONDecodeError:
            # 解析失败，记录日志但不影响文档上传
            from sag.utils import get_logger
            logger = get_logger("api.documents")
            logger.warning(f"实体类型配置解析失败: {entity_types}")
        except Exception as e:
            # 创建实体类型失败，记录日志但不影响文档上传
            from sag.utils import get_logger
            logger = get_logger("api.documents")
            logger.error(f"创建文档专属实体类型失败: {e}", exc_info=True)

    # 如果启用自动处理，添加后台任务
    if auto_process and result.article_id:
        background_tasks.add_task(
            service.process_document_async,
            article_id=result.article_id,
            source_config_id=source_config_id,
            file_path=result.file_path,
            task_id=result.task_id,  # 传递 task_id
            background=background,
        )

    return SuccessResponse(
        data=result,
        message=result.message,
    )


@router.post(
    "/sources/{source_config_id}/documents/upload-multiple",
    response_model=SuccessResponse[List[DocumentUploadResponse]],
    status_code=status.HTTP_201_CREATED,
)
async def upload_multiple_documents(
    source_config_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., description="文档文件列表"),
    background: Optional[str] = Form(None),
    auto_process: bool = Form(True),
    db: AsyncSession = Depends(get_db),
):
    """
    批量上传文档（异步处理）

    **功能**：
    - 一次上传多个文档
    - 后台自动处理所有文档
    """
    service = DocumentService(db)

    results = []
    for file in files:
        try:
            result = await service.upload_document(
                source_config_id=source_config_id,
                file=file,
                background=background,
                auto_process=auto_process,
            )
            results.append(result)

            # 如果启用自动处理，添加后台任务
            if auto_process and result.article_id:
                background_tasks.add_task(
                    service.process_document_async,
                    article_id=result.article_id,
                    source_config_id=source_config_id,
                    file_path=result.file_path,
                    background=background,
                )

        except Exception as e:
            # 记录错误但继续处理其他文件
            results.append(
                DocumentUploadResponse(
                    filename=file.filename or "unknown",
                    file_path="",
                    success=False,
                    message=str(e),
                )
            )

    return SuccessResponse(
        data=results,
        message=f"成功上传 {len([r for r in results if r.success])} / {len(files)} 个文档",
    )


@router.get("/documents", response_model=PaginatedResponse[DocumentResponse])
async def list_all_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    source_config_id: Optional[str] = Query(None, description="可选：按信息源筛选"),
    status_param: Optional[str] = Query(
        None, alias="status", description="状态筛选: PENDING, COMPLETED, FAILED"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    获取所有文档列表（跨信息源）

    **功能**：
    - 查询所有文档，支持按信息源和状态筛选
    - 用于全局实体类型创建时选择文档范围
    - 返回文档的基本信息（包括 source_config_id）

    **参数**：
    - page: 页码（从1开始）
    - page_size: 每页数量（1-1000）
    - source_config_id: 可选，按信息源筛选
    - status: 可选，按状态筛选（PENDING, COMPLETED, FAILED）
    """
    service = DocumentService(db)
    documents, total = await service.list_all_documents(
        page=page,
        page_size=page_size,
        source_config_id=source_config_id,
        status_filter=status_param,
    )
    return PaginatedResponse.create(
        data=documents,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/sources/{source_config_id}/documents", response_model=PaginatedResponse[DocumentResponse])
async def list_documents(
    source_config_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_param: Optional[str] = Query(
        None, alias="status", description="状态筛选: PENDING, COMPLETED, FAILED"
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    获取文档列表

    **功能**：
    - 查询信息源下的所有文档（文章）
    - 支持按状态筛选
    """
    service = DocumentService(db)
    documents, total = await service.list_documents(
        source_config_id=source_config_id,
        page=page,
        page_size=page_size,
        status_filter=status_param,
    )
    return PaginatedResponse.create(
        data=documents,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{article_id}", response_model=SuccessResponse[DocumentResponse])
async def get_document(
    article_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    获取文档详情

    **返回**：
    - 文档基本信息
    - 处理状态
    - 片段数量
    - 事项数量
    """
    service = DocumentService(db)
    document = await service.get_document(article_id)
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档不存在: {article_id}",
        )
    return SuccessResponse(data=document)


@router.delete("/documents/{article_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    article_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    删除文档

    **注意**：
    - 会级联删除所有相关数据
    - 包括文章片段、事项、实体关联等
    - 此操作不可恢复
    """
    service = DocumentService(db)
    success = await service.delete_document(article_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档不存在: {article_id}",
        )


@router.get(
    "/documents/{article_id}/sections",
    response_model=SuccessResponse[List[ArticleSectionResponse]],
    summary="获取文档的片段列表",
)
async def get_document_sections(
    article_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    获取指定文档的所有片段

    **功能**：
    - 返回文档的所有片段（按 rank 排序）
    - 包含片段标题、内容等信息

    **参数**：
    - article_id: 文档ID

    **返回**：
    - 片段列表（按 rank 升序排列）
    """
    service = DocumentService(db)
    sections = await service.get_document_sections(article_id)
    return SuccessResponse(data=sections)


@router.get(
    "/documents/{article_id}/events",
    response_model=SuccessResponse[List[SourceEventResponse]],
    summary="获取文档的事项列表",
)
async def get_document_events(
    article_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    获取指定文档的所有事项

    **功能**：
    - 返回文档的所有事项（按 rank 排序）
    - 包含事项标题、摘要、内容等信息

    **参数**：
    - article_id: 文档ID

    **返回**：
    - 事项列表（按 rank 升序排列）
    """
    service = DocumentService(db)
    events, sections_dict = await service.get_document_events(article_id)

    # 使用新方法转换，包含实体信息和完整片段
    response_data = [SourceEventResponse.from_orm_with_entities(event, sections_dict) for event in events]

    return SuccessResponse(data=response_data)


@router.put(
    "/documents/{article_id}",
    response_model=SuccessResponse[DocumentResponse],
    summary="更新文档信息",
)
async def update_document(
    article_id: str,
    data: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    更新指定文档的信息

    **功能**：
    - 更新文档的标题、摘要、标签等信息
    - 支持部分更新（只更新提供的字段）

    **参数**：
    - article_id: 文档ID
    - data: 更新的数据

    **返回**：
    - 更新后的文档信息
    """
    service = DocumentService(db)
    document = await service.update_document(article_id, data.model_dump(exclude_none=True))

    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文档不存在: {article_id}",
        )

    return SuccessResponse(data=document)
