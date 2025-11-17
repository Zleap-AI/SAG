"""
SAG 引擎核心类

提供标准化的数据处理任务引擎
"""

import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from sag.core.prompt.manager import get_prompt_manager
from sag.db import SourceChunk, SourceConfig, get_session_factory
from sag.engine.config import (
    ModelConfig,
    OutputConfig,
    TaskConfig,
)
from sag.engine.enums import LogLevel, TaskStage, TaskStatus
from sag.engine.models import StageResult, TaskLog, TaskResult
from sag.modules.extract.config import ExtractBaseConfig, ExtractConfig
from sag.modules.extract.extractor import EventExtractor
from sag.modules.load.config import (
    LoadBaseConfig, 
    DocumentLoadConfig, 
    ConversationLoadConfig,
    LoadResult
)
from sag.modules.load.loader import DocumentLoader, ConversationLoader
from sag.modules.search.config import SearchBaseConfig, SearchConfig
from sag.modules.search.searcher import EventSearcher
from sag.utils import get_logger, setup_logging

logger = get_logger("sag.engine")


class SAGEngine:
    """
    SAG任务引擎

    支持三个独立阶段：Load、Extract、Search
    可以单独执行，也可以链式组合

    使用示例：
        # 方式1：统一配置
        >>> engine = SAGEngine(task_config=TaskConfig(...), model_config=ModelConfig(...))
        >>> result = engine.run()

        # 方式2：独立执行各阶段
        >>> engine = SAGEngine(source_config_id="my-source")
        >>> engine.load(LoadBaseConfig(path="doc.md"))
        >>> engine.extract(ExtractBaseConfig(parallel=True))
        >>> engine.search(SearchBaseConfig(query="查找AI相关内容"))
        >>> result = engine.get_result()

        # 方式3：链式调用
        >>> result = (
        ...     SAGEngine(source_config_id="my-source")
        ...     .load(LoadBaseConfig(path="doc.md"))
        ...     .extract(ExtractBaseConfig())
        ...     .search(SearchBaseConfig(query="..."))
        ...     .get_result()
        ... )
    """

    def __init__(
        self,
        task_config: Optional[TaskConfig] = None,
        model_config: Optional[ModelConfig] = None,
        source_config_id: Optional[str] = None,
        auto_setup_logging: bool = True,
    ):
        """
        初始化引擎

        Args:
            task_config: 任务配置（如果提供，使用统一配置模式）
            model_config: ModelConfig配置（如果不提供，从.env读取默认配置）
            source_config_id: 信息源ID（简化初始化）
            auto_setup_logging: 是否自动设置日志
        """
        if auto_setup_logging:
            setup_logging()

        # 配置
        self.task_config = task_config

        # 转换 model_config 为字典（如果有）
        self.model_config_dict = model_config.model_dump() if model_config else None

        # 生成任务ID
        self.task_id = str(uuid.uuid4())

        # 初始化组件
        self.prompt_manager = get_prompt_manager()
        self.session_factory = get_session_factory()

        self.document_loader = DocumentLoader()
        self.conversation_loader = ConversationLoader()
        self.extractor = EventExtractor(
            prompt_manager=self.prompt_manager,
            model_config=self.model_config_dict
        )
        self.searcher = EventSearcher(
            prompt_manager=self.prompt_manager,
            model_config=self.model_config_dict
        )

        # 初始化结果
        task_name = task_config.task_name if task_config else "SAG任务"
        self.result = TaskResult(
            task_id=self.task_id, task_name=task_name, status=TaskStatus.PENDING
        )

        # 状态上下文
        self._source_config_id: Optional[str] = (
            source_config_id or (task_config.source_config_id if task_config else None)
        )
        self._load_result: Optional[LoadResult] = None  # Load结果
        self._start_time: Optional[float] = None

        self._log(TaskStage.INIT, LogLevel.INFO, f"引擎初始化完成: {self.task_id}")

    def _log(
        self,
        stage: TaskStage,
        level: LogLevel,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """记录日志"""
        log = TaskLog(stage=stage, level=level, message=message, extra=extra)
        self.result.logs.append(log)

        # 根据配置决定是否打印
        if not self.task_config or self.task_config.output.print_logs:
            log_func = getattr(logger, level.value)
            log_func(message, extra=extra)

    def _update_status(self, status: TaskStatus):
        """更新任务状态"""
        self.result.status = status

    async def _ensure_source(self) -> str:
        """确保信息源存在"""
        if not self._source_config_id:
            self._source_config_id = str(uuid.uuid4())
            self._log(TaskStage.INIT, LogLevel.INFO, f"自动创建信息源: {self._source_config_id}")

        async with self.session_factory() as session:
            result_db = await session.execute(
                select(SourceConfig).where(SourceConfig.id == self._source_config_id)
            )
            source = result_db.scalar_one_or_none()

            if not source:
                source_name = (
                    self.task_config.source_name
                    if self.task_config and self.task_config.source_name
                    else f"SAG-{uuid.uuid4().hex[:8]}"
                )
                source = SourceConfig(
                    id=self._source_config_id,
                    name=source_name,
                    description=f"由SAG引擎创建 (Task: {self.task_id})",
                    config={"task_id": self.task_id},
                )
                session.add(source)
                await session.commit()

        self.result.source_config_id = self._source_config_id
        return self._source_config_id

    def _apply_defaults(self, config):
        """
        应用任务级默认配置到阶段配置
        
        如果阶段配置未设置，则自动继承任务配置的值
        """
        if not self.task_config:
            return config
        
        # 继承 background
        if hasattr(config, 'background') and not config.background:
            config.background = self.task_config.background
        
        # 继承 source_config_id
        if hasattr(config, 'source_config_id') and not config.source_config_id:
            config.source_config_id = self.task_config.source_config_id
        
        return config

    # === Load阶段 ===

    def load(self, config: LoadBaseConfig) -> "SAGEngine":
        """加载文档（同步接口，支持链式调用）"""
        import asyncio

        asyncio.run(self.load_async(config))
        return self

    async def load_async(self, config):
        """
        加载数据（异步接口）
        
        支持：
        - DocumentLoadConfig: 加载文档（文件或数据库）
        - ConversationLoadConfig: 加载会话（数据库）
        """
        stage_start = time.time()
        self._update_status(TaskStatus.LOADING)
        self._log(TaskStage.LOAD, LogLevel.INFO, "开始加载数据")

        try:
            source_config_id = await self._ensure_source()

            # 应用任务级默认配置
            config.source_config_id = config.source_config_id or source_config_id
            self._apply_defaults(config)

            # 根据配置类型选择loader
            if isinstance(config, ConversationLoadConfig):
                load_result = await self.conversation_loader.load(config)
            elif isinstance(config, DocumentLoadConfig):
                load_result = await self.document_loader.load(config)
            else:
                raise ValueError(
                    f"不支持的配置类型: {type(config).__name__}。"
                    f"请使用DocumentLoadConfig或ConversationLoadConfig"
                )

            # 保存LoadResult到上下文
            self._load_result = load_result

            # 保存结果到TaskResult
            self.result.load_result = StageResult(
                stage=TaskStage.LOAD,
                status="success",
                data_ids=load_result.chunk_ids,
                data_full=[],  # 不保存完整数据，避免过大
                stats={
                    "source_id": load_result.source_id,
                    "source_type": load_result.source_type,
                    "chunk_count": load_result.chunk_count,
                    "title": load_result.title,
                    **load_result.extra
                },
                duration=time.time() - stage_start,
            )

            self._log(
                TaskStage.LOAD, 
                LogLevel.INFO, 
                f"加载完成: {load_result.source_type} "
                f"source_id={load_result.source_id}, "
                f"chunks={load_result.chunk_count}"
            )

        except Exception as e:
            self.result.load_result = StageResult(
                stage=TaskStage.LOAD,
                status="failed",
                error=str(e),
                duration=time.time() - stage_start,
            )
            self._log(TaskStage.LOAD, LogLevel.ERROR, f"加载失败: {e}")
            if self.task_config and self.task_config.fail_fast:
                raise

    # === Extract阶段 ===

    def extract(self, config: Optional[ExtractBaseConfig] = None) -> "SAGEngine":
        """提取事项（同步接口，支持链式调用）"""
        import asyncio

        asyncio.run(self.extract_async(config))
        return self

    async def extract_async(self, config: Optional[ExtractBaseConfig] = None):
        """
        提取事项（异步接口）
        
        前提：必须先执行Load阶段
        """
        # 验证前置条件
        if not self._load_result:
            self._log(
                TaskStage.EXTRACT, 
                LogLevel.WARNING, 
                "未加载数据，跳过提取阶段"
            )
            return

        if not self._load_result.chunk_ids:
            self._log(
                TaskStage.EXTRACT, 
                LogLevel.WARNING, 
                "Load阶段没有生成chunks，跳过提取阶段"
            )
            return

        stage_start = time.time()
        self._update_status(TaskStatus.EXTRACTING)
        self._log(TaskStage.EXTRACT, LogLevel.INFO, "开始提取事项")

        try:
            source_config_id = await self._ensure_source()
            config = config or ExtractBaseConfig()
            
            # 应用任务级默认配置
            self._apply_defaults(config)

            # 组装完整配置：使用Load结果的chunk_ids
            extract_config = ExtractConfig(
                source_config_id=source_config_id,
                chunk_ids=self._load_result.chunk_ids,
                **config.model_dump()
            )

            events = await self.extractor.extract(extract_config)

            # 保存结果
            self.result.extract_result = StageResult(
                stage=TaskStage.EXTRACT,
                status="success",
                data_ids=[e.id for e in events],
                data_full=[
                    {
                        "id": e.id,
                        "title": e.title,
                        "summary": e.summary,
                        "content": e.content,
                        "entities": [
                            {"name": a.entity.name, "type": a.entity.type}
                            for a in e.event_associations
                        ],
                    }
                    for e in events
                ],
                stats={
                    "event_count": len(events),
                    "chunk_count": len(self._load_result.chunk_ids),
                    "events_per_chunk": round(
                        len(events) / len(self._load_result.chunk_ids), 2
                    ) if self._load_result.chunk_ids else 0
                },
                duration=time.time() - stage_start,
            )

            self._log(
                TaskStage.EXTRACT, 
                LogLevel.INFO, 
                f"提取完成: {len(events)} 个事项"
            )

        except Exception as e:
            self.result.extract_result = StageResult(
                stage=TaskStage.EXTRACT,
                status="failed",
                error=str(e),
                duration=time.time() - stage_start,
            )
            self._log(TaskStage.EXTRACT, LogLevel.ERROR, f"提取失败: {e}")
            if self.task_config and self.task_config.fail_fast:
                raise

    # === Search阶段 ===

    def search(self, config: SearchBaseConfig) -> "SAGEngine":
        """搜索事项（同步接口，支持链式调用）"""
        import asyncio

        asyncio.run(self.search_async(config))
        return self

    async def search_async(self, config: SearchBaseConfig):
        """搜索事项（异步接口）"""
        if not config.query:
            self._log(TaskStage.SEARCH, LogLevel.WARNING, "未提供检索目标，跳过搜索阶段")
            return

        stage_start = time.time()
        self._update_status(TaskStatus.SEARCHING)
        self._log(TaskStage.SEARCH, LogLevel.INFO, f"开始搜索: {config.query}")

        try:
            # 应用任务级默认配置
            self._apply_defaults(config)
            
            # 检查是否已经是完整的 SearchConfig
            if isinstance(config, SearchConfig):
                search_config = config
            else:
                # 需要组装完整配置
                source_config_id = await self._ensure_source()
                search_config = SearchConfig(
                    source_config_id=source_config_id,
                    article_id=None,
                    **config.model_dump()
                )

            # 搜索返回字典格式: {"events": [...], "clues": [...]}
            # 注意：clues 现在是 from-to 格式的详细线索列表
            search_result = await self.searcher.search(search_config)
            matched_events = search_result.get("events", [])
            search_clues = search_result.get("clues", [])  # ✅ 改为列表格式

            # 保存结果
            self.result.search_result = StageResult(
                stage=TaskStage.SEARCH,
                status="success",
                data_ids=[e.id for e in matched_events],
                data_full=[
                    {
                        "id": e.id,
                        "title": e.title,
                        "summary": e.summary,
                        "content": e.content,
                    }
                    for e in matched_events
                ],
                stats={
                    "matched_count": len(matched_events),
                    "clues": search_clues  # 保存clues到stats中
                },
                duration=time.time() - stage_start,
            )

            self._log(
                TaskStage.SEARCH, LogLevel.INFO, f"搜索完成: {len(matched_events)} 个匹配事项"
            )

        except Exception as e:
            self.result.search_result = StageResult(
                stage=TaskStage.SEARCH,
                status="failed",
                error=str(e),
                duration=time.time() - stage_start,
            )
            self._log(TaskStage.SEARCH, LogLevel.ERROR, f"搜索失败: {e}")
            if self.task_config and self.task_config.fail_fast:
                raise

    # === 统一执行 ===

    def run(self) -> TaskResult:
        """运行任务（根据配置执行相应阶段）"""
        import asyncio

        return asyncio.run(self.run_async())

    async def run_async(self) -> TaskResult:
        """运行任务（异步）"""
        self._start_time = time.time()
        self.result.start_time = datetime.utcnow()

        try:
            if not self.task_config:
                raise ValueError("未提供task_config，请使用独立的load/extract/search方法")

            # 执行启用的阶段
            if self.task_config.load:
                await self.load_async(self.task_config.load)

            if self.task_config.extract:
                await self.extract_async(self.task_config.extract)

            if self.task_config.search:
                await self.search_async(self.task_config.search)

            self._update_status(TaskStatus.COMPLETED)
            self._log(TaskStage.OUTPUT, LogLevel.INFO, "任务执行成功")

        except Exception as e:
            self._update_status(TaskStatus.FAILED)
            self.result.error = str(e)
            self._log(TaskStage.OUTPUT, LogLevel.ERROR, f"任务执行失败: {e}")

        finally:
            self.result.end_time = datetime.utcnow()
            self.result.duration = time.time() - self._start_time
            self._compute_stats()

        return self.result

    # ============ 便捷属性 ============
    
    @property
    def chunk_ids(self) -> Optional[List[str]]:
        """获取当前chunk_ids"""
        return self._load_result.chunk_ids if self._load_result else None
    
    @property
    def source_id(self) -> Optional[str]:
        """获取当前source_id（article_id或conversation_id）"""
        return self._load_result.source_id if self._load_result else None
    
    @property
    def source_type(self) -> Optional[str]:
        """获取当前source_type（ARTICLE或CONVERSATION）"""
        return self._load_result.source_type if self._load_result else None
    
    # ============ 工具方法 ============
    
    def _compute_stats(self):
        """计算统计信息"""
        stats = {}
        if self.result.load_result:
            stats["chunks"] = len(self.result.load_result.data_ids)
        if self.result.extract_result:
            stats["events"] = len(self.result.extract_result.data_ids)
        if self.result.search_result:
            stats["matched_events"] = len(self.result.search_result.data_ids)
        stats["log_count"] = len(self.result.logs)
        self.result.stats = stats

    def get_result(self) -> TaskResult:
        """获取结果"""
        return self.result

    def output(self, output_config: Optional[OutputConfig] = None) -> Optional[str]:
        """输出结果"""
        config = output_config or (
            self.task_config.output if self.task_config else OutputConfig()
        )
        if config.format == "json":
            content = self.result.to_json(config)
        else:
            raise ValueError(f"不支持的格式: {config.format}")

        if config.export_path:
            config.export_path.write_text(content, encoding="utf-8")
            logger.info(f"结果已导出到: {config.export_path}")
            return None
        return content

