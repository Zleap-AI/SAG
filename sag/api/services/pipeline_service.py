"""流程服务"""

import uuid
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sag import SAGEngine, TaskConfig
from sag.api.schemas.common import TaskStatusResponse
from sag.api.schemas.pipeline import PipelineRequest, PipelineResponse
from sag.db.models import Task


class PipelineService:
    """流程服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_task(self, request: PipelineRequest) -> str:
        """创建任务"""
        task_id = str(uuid.uuid4())

        # 创建数据库记录
        task = Task(
            id=task_id,
            task_type="pipeline_run",
            status="pending",
            progress=Decimal("0.00"),
            message="任务已创建",
            source_config_id=request.source_config_id,
            extra_data={
                "request": request.model_dump(),
            },
        )
        self.db.add(task)
        await self.db.commit()
        await self.db.refresh(task)

        return task_id

    async def execute_pipeline(self, task_id: str, request: PipelineRequest):
        """执行流程（异步）"""
        try:
            # 查询任务
            result = await self.db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return

            # 更新状态为运行中
            task.status = "processing"
            task.message = "正在执行..."
            await self.db.commit()

            # 构建 TaskConfig
            task_config = TaskConfig(
                task_name=request.task_name,
                task_description=request.task_description,
                source_config_id=request.source_config_id,
                source_name=request.source_name,
                background=request.background,
                load=request.load,
                extract=request.extract,
                search=request.search,
                output=request.output,
                fail_fast=request.fail_fast,
            )

            # 创建引擎并执行
            engine = SAGEngine(
                task_config=task_config,
                model_config=request.llm,
            )

            engine_result = await engine.run_async()

            # 更新任务结果
            if engine_result.is_success():
                task.status = "completed"
                task.progress = Decimal("100.00")
                task.message = "任务完成"
                
                # 🔧 只存储摘要信息，避免数据过大
                full_result = engine_result.to_dict(request.output)
                
                # 构建精简的结果（只保留关键信息）
                task.result = {
                    "task_id": full_result.get("task_id"),
                    "task_name": full_result.get("task_name"),
                    "status": full_result.get("status"),
                    "source_config_id": full_result.get("source_config_id"),
                    "article_id": full_result.get("article_id"),
                    "start_time": full_result.get("start_time"),
                    "end_time": full_result.get("end_time"),
                    "duration": full_result.get("duration"),
                    "stats": full_result.get("stats", {}),
                }
                
                # 各阶段结果：只保留 ID 列表和统计信息
                if "load" in full_result:
                    task.result["load"] = {
                        "status": full_result["load"].get("status"),
                        "stats": full_result["load"].get("stats", {}),
                    }
                
                if "extract" in full_result:
                    extract_data = full_result["extract"]
                    task.result["extract"] = {
                        "status": extract_data.get("status"),
                        "stats": extract_data.get("stats", {}),
                    }
                    
                    # 只保留事项 ID 列表（不保存完整数据）
                    if "results" in extract_data:
                        results = extract_data["results"]
                        if isinstance(results, list):
                            # 如果是 data_full（字典列表），只提取 ID
                            if results and isinstance(results[0], dict):
                                task.result["extract"]["event_ids"] = [
                                    item.get("id") for item in results if item.get("id")
                                ]
                                task.result["extract"]["event_count"] = len(results)
                            # 如果是 data_ids（字符串列表），直接保存
                            else:
                                task.result["extract"]["event_ids"] = results
                                task.result["extract"]["event_count"] = len(results)
                
                if "search" in full_result:
                    search_data = full_result["search"]
                    task.result["search"] = {
                        "status": search_data.get("status"),
                        "stats": search_data.get("stats", {}),
                    }
                    
                    # 搜索结果也只保留摘要
                    if "results" in search_data:
                        results = search_data["results"]
                        if isinstance(results, list):
                            task.result["search"]["result_count"] = len(results)
                            # 可选：保留前10个结果的标题
                            if results and isinstance(results[0], dict):
                                task.result["search"]["top_results"] = [
                                    {
                                        "id": item.get("id"),
                                        "title": item.get("title"),
                                        "score": item.get("score")
                                    }
                                    for item in results[:10]  # 只保留前10个
                                ]
                
                # 日志：只保留最后 50 条
                if "logs" in full_result:
                    all_logs = full_result["logs"]
                    task.result["logs"] = all_logs[-50:] if len(all_logs) > 50 else all_logs
                    task.result["total_logs"] = len(all_logs)
                
                # 错误信息
                if full_result.get("error"):
                    task.result["error"] = full_result["error"]
                    
            else:
                task.status = "failed"
                task.error = engine_result.error
                task.message = f"任务失败: {engine_result.error}"

            await self.db.commit()

        except Exception as e:
            # 查询任务并更新状态
            result = await self.db.execute(select(Task).where(Task.id == task_id))
            task = result.scalar_one_or_none()
            if task:
                task.status = "failed"
                task.error = str(e)
                task.message = f"任务异常: {str(e)}"
                await self.db.commit()

    async def execute_pipeline_sync(
        self, request: PipelineRequest
    ) -> PipelineResponse:
        """同步执行流程"""
        # 构建 TaskConfig
        task_config = TaskConfig(
            task_name=request.task_name,
            task_description=request.task_description,
            source_config_id=request.source_config_id,
            source_name=request.source_name,
            background=request.background,
            load=request.load,
            extract=request.extract,
            search=request.search,
            output=request.output,
            fail_fast=request.fail_fast,
        )

        # 创建引擎并执行
        engine = SAGEngine(
            task_config=task_config,
            model_config=request.llm,
        )

        result = await engine.run_async()

        # 构建响应
        response = PipelineResponse(
            task_id=result.task_id,
            task_name=result.task_name,
            status=result.status.value,
            source_config_id=result.source_config_id,
            article_id=result.article_id,
            stats=result.stats,
            logs=[
                {
                    "timestamp": log.timestamp.isoformat(),
                    "stage": log.stage.value,
                    "level": log.level.value,
                    "message": log.message,
                    "extra": log.extra,
                }
                for log in result.logs
            ],
            error=result.error,
            start_time=result.start_time.isoformat() if result.start_time else None,
            end_time=result.end_time.isoformat() if result.end_time else None,
            duration=result.duration,
        )

        # 添加各阶段结果
        if result.load_result:
            response.load_result = {
                "status": result.load_result.status,
                "data_ids": result.load_result.data_ids,
                "stats": result.load_result.stats,
            }

        if result.extract_result:
            response.extract_result = {
                "status": result.extract_result.status,
                "data_ids": result.extract_result.data_ids,
                "stats": result.extract_result.stats,
            }

        if result.search_result:
            response.search_result = {
                "status": result.search_result.status,
                "data_full": result.search_result.data_full,
                "stats": result.search_result.stats,
            }

        return response

    async def get_task_status(self, task_id: str) -> Optional[TaskStatusResponse]:
        """获取任务状态"""
        result = await self.db.execute(
            select(Task)
            .options(selectinload(Task.source), selectinload(Task.article))
            .where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()

        if not task:
            return None

        return TaskStatusResponse(
            task_id=task.id,
            task_type=task.task_type,
            status=task.status,
            progress=float(task.progress) if task.progress else None,
            message=task.message,
            result=task.result,
            error=task.error,
            created_time=task.created_time.isoformat() + 'Z' if task.created_time else None,
            updated_time=task.updated_time.isoformat() + 'Z' if task.updated_time else None,
            source_config_id=task.source_config_id,
            source_name=task.source.name if task.source else None,
            article_id=task.article_id,
            article_title=task.article.title if task.article else None,
        )

    async def list_tasks(
        self,
        source_config_id: Optional[str] = None,
        status_filter: Optional[str] = None,
        search_query: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[TaskStatusResponse], int]:
        """获取任务列表"""
        # 构建查询
        query = select(Task).options(selectinload(
            Task.source), selectinload(Task.article))

        # 添加过滤条件
        if source_config_id:
            query = query.where(Task.source_config_id == source_config_id)
        if status_filter:
            query = query.where(Task.status == status_filter)
        
        # 🆕 搜索过滤（任务ID、消息、信息源名称、文档标题）
        if search_query:
            from sqlalchemy import or_
            from sag.db.models import Source, Article
            search_pattern = f"%{search_query}%"
            query = query.where(
                or_(
                    Task.id.like(search_pattern),
                    Task.message.like(search_pattern),
                    # 通过关联查询搜索信息源名称
                    Task.source.has(Source.name.like(search_pattern)),
                    # 通过关联查询搜索文档标题
                    Task.article.has(Article.title.like(search_pattern))
                )
            )

        # 按创建时间倒序排列
        query = query.order_by(Task.created_time.desc())

        # 执行查询获取所有任务（用于计算总数）
        result = await self.db.execute(query)
        all_tasks = result.scalars().all()
        total = len(all_tasks)

        # 分页
        start = (page - 1) * page_size
        end = start + page_size
        page_tasks = all_tasks[start:end]

        return [
            TaskStatusResponse(
                task_id=task.id,
                task_type=task.task_type,
                status=task.status,
                progress=float(task.progress) if task.progress else None,
                message=task.message,
                result=task.result,
                error=task.error,
                created_time=task.created_time.isoformat() + 'Z' if task.created_time else None,
                updated_time=task.updated_time.isoformat() + 'Z' if task.updated_time else None,
                source_config_id=task.source_config_id,
                source_name=task.source.name if task.source else None,
                article_id=task.article_id,
                article_title=task.article.title if task.article else None,
            )
            for task in page_tasks
        ], total

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务"""
        result = await self.db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

        if not task:
            return False

        if task.status in ["completed", "failed"]:
            return False

        task.status = "cancelled"
        task.message = "任务已取消"
        await self.db.commit()

        return True

    async def batch_delete_tasks(
        self,
        task_ids: Optional[List[str]] = None,
        status_filter: Optional[List[str]] = None,
    ) -> int:
        """批量删除任务

        Args:
            task_ids: 指定要删除的任务ID列表
            status_filter: 按状态过滤删除（如 ["completed", "failed"]）

        Returns:
            删除的任务数量
        """
        from sqlalchemy import delete

        # 构建删除条件
        conditions = []

        if task_ids:
            conditions.append(Task.id.in_(task_ids))

        if status_filter:
            conditions.append(Task.status.in_(status_filter))

        if not conditions:
            return 0

        # 执行删除
        stmt = delete(Task)
        for condition in conditions:
            stmt = stmt.where(condition)

        result = await self.db.execute(stmt)
        await self.db.commit()

        return result.rowcount

    async def get_tasks_stats(self) -> dict:
        """获取任务统计信息"""
        from sqlalchemy import func

        # 总数
        total_result = await self.db.execute(select(func.count()).select_from(Task))
        total = total_result.scalar() or 0

        # 按状态分组统计
        stats_result = await self.db.execute(
            select(Task.status, func.count()).group_by(Task.status)
        )
        status_stats = {row[0]: row[1] for row in stats_result.all()}

        # 按类型分组统计
        type_result = await self.db.execute(
            select(Task.task_type, func.count()).group_by(Task.task_type)
        )
        type_stats = {row[0]: row[1] for row in type_result.all()}

        return {
            "total": total,
            "by_status": {
                "pending": status_stats.get("pending", 0),
                "processing": status_stats.get("processing", 0),
                "completed": status_stats.get("completed", 0),
                "failed": status_stats.get("failed", 0),
                "cancelled": status_stats.get("cancelled", 0),
            },
            "by_type": {
                "document_upload": type_stats.get("document_upload", 0),
                "pipeline_run": type_stats.get("pipeline_run", 0),
            },
        }
