"""文档服务"""

import shutil
import uuid
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime

from fastapi import UploadFile
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from sag.api.schemas.document import DocumentResponse, DocumentUploadResponse
from sag.db.models import Article, ArticleSection, SourceEvent, Task


class DocumentService:
    """文档服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def upload_document(
        self,
        source_config_id: str,
        file: UploadFile,
        background: Optional[str] = None,
        auto_process: bool = True,
    ) -> DocumentUploadResponse:
        """上传文档（立即返回）"""
        # 1. 创建上传目录
        upload_dir = Path("./uploads") / source_config_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        # 2. 生成文件名
        file_ext = Path(file.filename or "unknown").suffix
        file_id = str(uuid.uuid4())
        filename = f"{file_id}{file_ext}"
        file_path = upload_dir / filename

        # 3. 保存文件
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # 4. 创建占位 Article（status=PENDING）
        article = Article(
            id=file_id,
            source_config_id=source_config_id,
            title=file.filename or "未命名文档",
            status="PENDING",
        )
        self.db.add(article)

        # 5. 如果启用自动处理，创建任务记录
        task_id = None
        if auto_process:
            task_id = str(uuid.uuid4())
            task = Task(
                id=task_id,
                task_type="document_upload",
                status="pending",
                progress=Decimal("0.00"),
                message="文档上传成功，等待处理",
                source_config_id=source_config_id,
                article_id=file_id,
                extra_data={
                    "filename": file.filename,
                    "file_path": str(file_path),
                    "background": background,
                },
            )
            self.db.add(task)

        await self.db.commit()

        # 6. 立即返回（不等待处理）
        message = "文档上传成功"
        if auto_process:
            message = "文档上传成功，正在后台处理..."

        return DocumentUploadResponse(
            filename=file.filename or "unknown",
            file_path=str(file_path),
            article_id=file_id,
            task_id=task_id,
            success=True,
            message=message,
        )

    async def process_document_async(
        self,
        article_id: str,
        source_config_id: str,
        file_path: str,
        task_id: Optional[str] = None,
        background: Optional[str] = None,
    ):
        """后台处理文档（异步执行）"""
        try:
            # 1. 更新任务状态为 PROCESSING
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "processing"
                    task.progress = Decimal("10.00")
                    task.message = "正在加载文档..."
                    await self.db.commit()

            # 2. 更新 Article 状态为 PROCESSING
            result = await self.db.execute(
                select(Article).where(Article.id == article_id)
            )
            article = result.scalar_one_or_none()
            if not article:
                print(f"❌ Article 不存在: {article_id}")
                if task_id:
                    task_result = await self.db.execute(select(Task).where(Task.id == task_id))
                    task = task_result.scalar_one_or_none()
                    if task:
                        task.status = "failed"
                        task.error = f"Article 不存在: {article_id}"
                        await self.db.commit()
                return

            article.status = "PROCESSING"
            await self.db.commit()

            # 3. 更新任务进度 - 开始提取
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.progress = Decimal("30.00")
                    task.message = "正在提取文档内容..."
                    await self.db.commit()

            # 4. 统一使用 SAGEngine 执行 Load + Extract
            from sag import SAGEngine, ExtractBaseConfig, DocumentLoadConfig
            from sag.engine.config import TaskConfig

            task_config = TaskConfig(
                task_name="文档处理",
                source_config_id=source_config_id,
                background=background,
                load=DocumentLoadConfig(
                    source_config_id=source_config_id,
                    path=file_path,
                    auto_vector=True,
                    article_id=article_id,  # 更新已存在的 Article
                ),
                extract=ExtractBaseConfig(parallel=True),
            )

            # 5. 更新任务进度 - 正在处理
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.progress = Decimal("50.00")
                    task.message = "正在分析和提取实体..."
                    await self.db.commit()

            engine = SAGEngine(task_config=task_config)
            engine_result = await engine.run_async()

            # 6. 更新任务进度 - 向量化
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.progress = Decimal("80.00")
                    task.message = "正在向量化..."
                    await self.db.commit()

            # 7. 整个流程完成后，设置状态为 COMPLETED
            result = await self.db.execute(
                select(Article).where(Article.id == article_id)
            )
            article = result.scalar_one_or_none()
            if article:
                article.status = "COMPLETED"
                await self.db.commit()

            # 8. 更新任务状态为完成
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "completed"
                    task.progress = Decimal("100.00")
                    task.message = "文档处理完成"
                    task.result = {
                        "article_id": article_id,
                        "status": "success",
                    }
                    await self.db.commit()

            print(f"✅ 文档处理成功: {article_id}")

        except Exception as e:
            # 9. 处理失败，更新状态
            print(f"❌ 文档处理失败: {article_id}: {e}")
            import traceback
            traceback.print_exc()

            # 更新 Article 状态
            result = await self.db.execute(
                select(Article).where(Article.id == article_id)
            )
            article = result.scalar_one_or_none()
            if article:
                article.status = "FAILED"
                article.error = str(e)
                await self.db.commit()

            # 更新任务状态
            if task_id:
                result = await self.db.execute(select(Task).where(Task.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "failed"
                    task.error = str(e)
                    task.message = f"文档处理失败: {str(e)}"
                    await self.db.commit()

    async def list_documents(
        self,
        source_config_id: str,
        page: int = 1,
        page_size: int = 20,
        status_filter: Optional[str] = None,
    ) -> Tuple[List[DocumentResponse], int]:
        """获取文档列表"""
        # 构建查询
        query = select(Article).where(Article.source_config_id == source_config_id)

        if status_filter:
            query = query.where(Article.status == status_filter)

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询
        query = query.offset((page - 1) * page_size).limit(page_size)
        query = query.order_by(Article.created_time.desc())
        
        result = await self.db.execute(query)
        articles = result.scalars().all()

        # 统计每个文档的片段和事项数量
        documents = []
        for article in articles:
            # 查询片段数量
            sections_count_result = await self.db.execute(
                select(func.count()).where(ArticleSection.article_id == article.id)
            )
            sections_count = sections_count_result.scalar() or 0

            # 查询事项数量
            events_count_result = await self.db.execute(
                select(func.count()).where(SourceEvent.article_id == article.id)
            )
            events_count = events_count_result.scalar() or 0

            doc_response = DocumentResponse.model_validate(article)
            doc_response.sections_count = sections_count
            doc_response.events_count = events_count

            # 处理 tags（从 JSON 转为 List）
            if article.tags:
                doc_response.tags = article.tags if isinstance(article.tags, list) else []

            documents.append(doc_response)

        return documents, total

    async def list_all_documents(
        self,
        page: int = 1,
        page_size: int = 100,
        source_config_id: Optional[str] = None,
        status_filter: Optional[str] = None,
    ) -> Tuple[List[DocumentResponse], int]:
        """获取所有文档列表（跨所有信息源，支持按信息源筛选）"""
        # 构建查询
        query = select(Article)

        # 可选：按信息源筛选
        if source_config_id:
            query = query.where(Article.source_config_id == source_config_id)

        # 可选：按状态筛选
        if status_filter:
            query = query.where(Article.status == status_filter)

        # 获取总数
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar() or 0

        # 分页查询和排序
        query = query.order_by(Article.created_time.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await self.db.execute(query)
        articles = result.scalars().all()

        # 统计每个文档的片段和事项数量
        documents = []
        for article in articles:
            # 查询片段数量
            sections_count_result = await self.db.execute(
                select(func.count()).where(ArticleSection.article_id == article.id)
            )
            sections_count = sections_count_result.scalar() or 0

            # 查询事项数量
            events_count_result = await self.db.execute(
                select(func.count()).where(SourceEvent.article_id == article.id)
            )
            events_count = events_count_result.scalar() or 0

            doc_response = DocumentResponse.model_validate(article)
            doc_response.sections_count = sections_count
            doc_response.events_count = events_count

            # 处理 tags（从 JSON 转为 List）
            if article.tags:
                doc_response.tags = article.tags if isinstance(article.tags, list) else []

            documents.append(doc_response)

        return documents, total

    async def get_document(self, article_id: str) -> Optional[DocumentResponse]:
        """获取文档详情"""
        result = await self.db.execute(select(Article).where(Article.id == article_id))
        article = result.scalar_one_or_none()

        if not article:
            return None

        # 统计片段和事项数量
        sections_count_result = await self.db.execute(
            select(func.count()).where(ArticleSection.article_id == article_id)
        )
        sections_count = sections_count_result.scalar() or 0

        events_count_result = await self.db.execute(
            select(func.count()).where(SourceEvent.article_id == article_id)
        )
        events_count = events_count_result.scalar() or 0

        doc_response = DocumentResponse.model_validate(article)
        doc_response.sections_count = sections_count
        doc_response.events_count = events_count

        # 处理 tags
        if article.tags:
            doc_response.tags = article.tags if isinstance(article.tags, list) else []

        return doc_response

    async def delete_document(self, article_id: str) -> bool:
        """删除文档"""
        result = await self.db.execute(select(Article).where(Article.id == article_id))
        article = result.scalar_one_or_none()

        if not article:
            return False

        await self.db.delete(article)
        await self.db.commit()

        return True

    async def get_document_sections(self, article_id: str) -> List[ArticleSection]:
        """获取文档的所有片段"""
        query = (
            select(ArticleSection)
            .where(ArticleSection.article_id == article_id)
            .order_by(ArticleSection.rank)
        )
        result = await self.db.execute(query)
        sections = result.scalars().all()
        return list(sections)

    async def get_document_events(self, article_id: str):
        """获取文档的所有事项和片段"""
        from sqlalchemy.orm import selectinload
        from sag.db.models import EventEntity, ArticleSection

        # 查询事项
        query = (
            select(SourceEvent)
            .where(SourceEvent.article_id == article_id)
            .order_by(SourceEvent.rank)
            .options(
                selectinload(SourceEvent.event_associations).selectinload(EventEntity.entity)
            )
        )
        result = await self.db.execute(query)
        events = result.scalars().all()

        # 查询所有片段（构建字典）
        sections_query = select(ArticleSection).where(ArticleSection.article_id == article_id)
        sections_result = await self.db.execute(sections_query)
        sections_dict = {section.id: section for section in sections_result.scalars().all()}

        return list(events), sections_dict

    async def update_document(self, article_id: str, data: dict) -> Optional[Article]:
        """更新文档信息"""
        result = await self.db.execute(select(Article).where(Article.id == article_id))
        article = result.scalar_one_or_none()

        if not article:
            return None

        # 只更新提供的字段
        update_data = {k: v for k, v in data.items() if v is not None}

        if update_data:
            for field, value in update_data.items():
                if hasattr(article, field):
                    setattr(article, field, value)

            article.updated_time = datetime.utcnow()
            await self.db.commit()
            await self.db.refresh(article)

        return article
