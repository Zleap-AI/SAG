"""
事项提取器

主控制器 - 协调单篇文档的提取流程
"""

import asyncio
import uuid
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from sag.core.ai.factory import get_embedding_client
from sag.core.ai.base import BaseLLMClient
from sag.core.prompt.manager import PromptManager
from sag.core.storage.elasticsearch import get_es_client
from sag.core.storage.repositories.entity_repository import EntityVectorRepository
from sag.core.storage.repositories.event_repository import EventVectorRepository
from sag.db import (
    SourceChunk,
    Entity,
    EntityType as DBEntityType,
    EventEntity,
    SourceEvent,
    get_session_factory,
)
from sag.exceptions import ExtractError
from sag.modules.extract.config import ExtractConfig
from sag.modules.extract.processor import EventProcessor
from sag.utils import estimate_tokens, get_logger

logger = get_logger("extract.extractor")


class EventExtractor:
    """事项提取器（主控制器）"""

    def __init__(
        self,
        prompt_manager: PromptManager,
        model_config: Optional[Dict] = None,
    ):
        """
        初始化事项提取器

        Args:
            prompt_manager: 提示词管理器
            model_config: LLM配置字典（可选）
                - 如果传入：使用该配置
                - 如果不传：自动从配置管理器获取 'extract' 场景配置
        """
        self.prompt_manager = prompt_manager
        self.model_config = model_config
        self._llm_client = None  # 延迟初始化
        self.session_factory = get_session_factory()
        self.logger = get_logger("extract.extractor")
        
        # ES相关（延迟初始化）
        self.es_client = None
        self.event_repo = None
        self.entity_repo = None
    
    async def _get_llm_client(self) -> BaseLLMClient:
        """获取LLM客户端（懒加载）"""
        if self._llm_client is None:
            from sag.core.ai.factory import create_llm_client
            
            self._llm_client = await create_llm_client(
                scenario='extract',
                model_config=self.model_config
            )
        
        return self._llm_client

    async def extract(self, config: ExtractConfig) -> List[SourceEvent]:
        """
        提取事项（统一入口 - 新架构）
        
        工作流程：
        1. 加载所有chunks
        2. 按max_concurrency并发处理（Semaphore控制）
        3. 每个chunk由一个ExtractorAgent处理
        4. 合并所有结果
        5. 保存到数据库 + Elasticsearch
        6. 更新源状态为已完成

        Args:
            config: 提取配置

        Returns:
            所有chunks提取的事项列表

        Example:
            config = ExtractConfig(
                source_config_id="source-uuid",
                chunk_ids=["chunk-1", "chunk-2", "chunk-3"],
                max_concurrency=3
            )
            events = await extractor.extract(config)
        """
        self.logger.info(
            f"开始批量提取: chunks={len(config.chunk_ids)}, "
            f"并发数={config.max_concurrency}"
        )

        try:
            # 1. 加载所有chunks
            chunks = await self._load_chunks(config.chunk_ids)

            if not chunks:
                self.logger.warning("没有找到可用的chunks")
                return []

            # 2. 并发处理chunks（每个chunk一个Agent）
            all_events = await self._process_chunks_with_agents(chunks, config)
            
            self.logger.info(
                f"批量提取完成: chunks={len(chunks)}, events={len(all_events)}"
            )
            
            # 3. 按原文顺序重新排序并分配全局 rank
            if all_events:
                # 创建 chunk_id -> chunk.rank 的映射
                chunk_rank_map = {chunk.id: chunk.rank for chunk in chunks}
                
                # 排序规则：
                # 1. 先按 chunk.rank（保证 chunk 之间的顺序）
                # 2. 再按事项的时间（会话）或 chunk 内 rank（文档）
                def sort_key(event):
                    chunk_order = chunk_rank_map.get(event.chunk_id, 9999)
                    
                    # 会话类型：按时间排序
                    if event.source_type == "CHAT" and event.start_time:
                        event_order = event.start_time
                    # 文档类型：按 chunk 内 rank 排序
                    else:
                        event_order = event.rank or 0
                    
                    return (chunk_order, event_order)
                
                all_events.sort(key=sort_key)
                
                # 重新分配全局连续 rank
                for i, event in enumerate(all_events):
                    event.rank = i
                
                self.logger.info(
                    f"事项已按原文顺序排序: chunks={len(chunks)}, events={len(all_events)}"
                )
            
            # 4. 保存到数据库（包括ES）
            if all_events:
                await self._save_events(all_events, config)
                
                # 5. 重新从数据库加载事项（带完整关系数据）
                # 解决跨 session 问题：保存后重新查询，确保所有关系正确加载
                event_ids = [e.id for e in all_events]
                all_events = await self._reload_events_with_relations(event_ids)
            else:
                self.logger.warning("没有提取到任何事项，跳过保存")
            
            # 6. 更新源状态为已完成
            if chunks:
                await self._update_source_status(chunks, status="COMPLETED")
            
            return all_events
            
        except Exception as e:
            # 提取失败时，更新状态为失败
            self.logger.error(f"提取失败: {e}", exc_info=True)
            try:
                chunks = await self._load_chunks(config.chunk_ids)
                if chunks:
                    await self._update_source_status(chunks, status="FAILED", error=str(e))
            except Exception as update_error:
                self.logger.error(f"更新失败状态时出错: {update_error}")
            
            raise ExtractError(f"提取失败: {e}") from e
    
    async def _load_chunks(self, chunk_ids: List[str]) -> List[SourceChunk]:
        """批量加载chunks（按rank排序）"""
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceChunk)
                .where(SourceChunk.id.in_(chunk_ids))
                .order_by(SourceChunk.rank)  # 🆕 按 rank 排序
            )
            chunks = list(result.scalars().all())
            
            if len(chunks) != len(chunk_ids):
                missing = set(chunk_ids) - {c.id for c in chunks}
                self.logger.warning(f"部分chunk不存在: {missing}")
            
            return chunks
    
    async def _process_chunks_with_agents(
        self,
        chunks: List[SourceChunk],
        config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        并发处理chunks（每个chunk一个Agent）
        
        使用asyncio.Semaphore控制并发数量：
        - 同时最多有max_concurrency个Agent在运行
        - 超出的chunk会自动排队等待
        - 一个chunk完成后，立即启动下一个
        
        Args:
            chunks: chunk列表
            config: 提取配置
        
        Returns:
            合并后的所有事项
        """
        semaphore = asyncio.Semaphore(config.max_concurrency)
        
        # 进度跟踪
        completed = 0
        success_count = 0
        failed_count = 0
        total = len(chunks)
        lock = asyncio.Lock()
        
        async def process_single_chunk(
            chunk: SourceChunk,
            index: int
        ) -> List[SourceEvent]:
            """处理单个chunk（带并发控制和进度统计）"""
            nonlocal completed, success_count, failed_count
            
            async with semaphore:  # 🔒 获取并发槽位（没有就等待）
                try:
                    self.logger.info(
                        f"[{index+1}/{total}] 开始处理: chunk_id={chunk.id}, "
                        f"type={chunk.source_type}"
                    )
                    
                    # 调用chunk级提取（使用ExtractorAgent）
                    events = await self.extract_from_chunk(chunk, config)
                    
                    # 更新进度
                    async with lock:
                        completed += 1
                        success_count += 1
                        progress = completed * 100 // total
                        
                    self.logger.info(
                        f"✅ [{index+1}/{total}] 完成 ({progress}%): "
                        f"chunk_id={chunk.id}, events={len(events)}"
                    )
                    
                    return events
                    
                except Exception as e:
                    # 更新失败统计
                    async with lock:
                        completed += 1
                        failed_count += 1
                        progress = completed * 100 // total
                        
                    self.logger.error(
                        f"❌ [{index+1}/{total}] 失败 ({progress}%): "
                        f"chunk_id={chunk.id}, error={e}",
                        exc_info=True
                    )
                    return []  # 失败返回空，不中断其他chunk
                # 🔓 离开时自动释放槽位
        
        # 并发执行所有chunk
        self.logger.info(
            f"🚀 启动并发提取: total={total}, concurrency={config.max_concurrency}"
        )
        
        tasks = [
            process_single_chunk(chunk, i)
            for i, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        
        # 合并结果
        all_events = []
        for events in results:
            all_events.extend(events)
        
        # 最终统计
        self.logger.info(
            f"📊 批量提取统计: 总数={total}, 成功={success_count}, "
            f"失败={failed_count}, 事项={len(all_events)}"
        )
        
        return all_events

    async def _load_sections(self, config: ExtractConfig) -> List[SourceChunk]:
        """
        加载来源片段

        Args:
            config: 提取配置

        Returns:
            来源片段列表
        """
        self.logger.debug(f"加载文章片段：article_id={config.article_id}")

        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceChunk)
                .where(SourceChunk.source_type == 'article')
                .where(SourceChunk.source_id == config.article_id)
                .order_by(SourceChunk.rank)
            )
            sections = result.scalars().all()

        self.logger.info(f"加载了 {len(sections)} 个来源片段")

        return list(sections)

    async def _ensure_entity_types(self, config: ExtractConfig) -> None:
        """
        确保实体类型已初始化到数据库

        Args:
            config: 提取配置
        """
        if not config.custom_entity_types:
            return

        self.logger.debug(f"初始化自定义实体类型：{len(config.custom_entity_types)} 个")

        async with self.session_factory() as session:
            for custom_type in config.custom_entity_types:
                result = await session.execute(
                    select(DBEntityType)
                    .where(DBEntityType.source_config_id == config.source_config_id)
                    .where(DBEntityType.type == custom_type.type)
                )
                existing = result.scalar_one_or_none()

                if not existing:
                    entity_type = DBEntityType(
                        id=str(uuid.uuid4()),
                        source_config_id=config.source_config_id,
                        type=custom_type.type,
                        name=custom_type.name,
                        description=custom_type.description,
                        weight=custom_type.weight,
                        is_active=True,
                        is_default=False,
                    )
                    session.add(entity_type)
                    self.logger.debug(f"创建自定义实体类型：{custom_type.type}")

            await session.commit()

    def _create_batches(
        self, sections: List[SourceChunk], config: ExtractConfig
    ) -> List[List[SourceChunk]]:
        """
        创建批次（智能分批，基于token限制）

        Args:
            sections: 来源片段列表
            config: 提取配置

        Returns:
            批次列表，每个批次是片段列表
        """
        batches = []
        current_batch = []
        current_tokens = 0

        for section in sections:
            section_text = f"{section.heading}\n{section.content}"
            section_tokens = estimate_tokens(section_text)

            will_exceed_token_limit = (
                current_tokens + section_tokens > config.max_tokens
            )
            will_exceed_section_limit = len(current_batch) >= config.max_sections

            if current_batch and (will_exceed_token_limit or will_exceed_section_limit):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(section)
            current_tokens += section_tokens

        if current_batch:
            batches.append(current_batch)

        self.logger.info(
            f"创建了 {len(batches)} 个批次，"
            f"平均每批 {len(sections) / len(batches):.1f} 个片段"
        )

        return batches

    async def _process_batches_parallel(
        self,
        sections: List[SourceChunk],
        processor: EventProcessor,
        config: ExtractConfig,
    ) -> List[SourceEvent]:
        """
        并发处理所有批次（两阶段处理）

        Args:
            sections: 来源片段列表
            processor: 事项处理器
            config: 提取配置

        Returns:
            所有批次提取的事项列表（包含实体关联）
        """
        batches = self._create_batches(sections, config)

        if not batches:
            self.logger.warning("没有可处理的批次")
            return []

        semaphore = asyncio.Semaphore(config.max_concurrency)
        
        # 进度统计
        completed_count = 0
        success_count = 0
        failed_count = 0
        progress_lock = asyncio.Lock()

        async def process_single_batch_extraction(
            batch_index: int, batch: List[SourceChunk]
        ) -> List[SourceEvent]:
            nonlocal completed_count, success_count, failed_count
            
            async with semaphore:
                try:
                    self.logger.debug(
                        f"批次 {batch_index + 1}/{len(batches)}: "
                        f"开始处理（阶段1），片段数={len(batch)}"
                    )
                    # 阶段1：提取事项（不含实体关联）
                    events = await processor.extract_events_without_entities(batch, batch_index)
                    
                    # 更新进度统计
                    async with progress_lock:
                        completed_count += 1
                        success_count += 1
                        progress_pct = completed_count * 100 // len(batches)
                        success_rate = success_count * 100 // completed_count
                        
                    self.logger.info(
                            f"📊 进度: {completed_count}/{len(batches)} ({progress_pct}%) | "
                            f"成功率: {success_rate}% ({success_count}✓/{failed_count}✗) | "
                            f"批次 {batch_index + 1}: 提取了 {len(events)} 个事项"
                    )
                    
                    return events
                except Exception as e:
                    # 更新失败统计
                    async with progress_lock:
                        completed_count += 1
                        failed_count += 1
                        progress_pct = completed_count * 100 // len(batches)
                        success_rate = success_count * 100 // completed_count if completed_count > 0 else 0
                        
                        self.logger.error(
                            f"❌ 进度: {completed_count}/{len(batches)} ({progress_pct}%) | "
                            f"成功率: {success_rate}% ({success_count}✓/{failed_count}✗) | "
                            f"批次 {batch_index + 1}: 处理失败: {e}"
                        )
                    
                    self.logger.error(
                        f"批次 {batch_index + 1} 详细错误",
                        exc_info=True,
                        extra={
                            "batch_index": batch_index,
                            "batch_size": len(batch),
                            "error_type": type(e).__name__,
                        },
                    )
                    # 返回空列表，但错误已被记录
                    return []

        # 阶段1：并发提取所有批次的事项（不含实体）
        self.logger.info(
            f"🚀 开始阶段1：并发处理 {len(batches)} 个批次（事项提取） - "
            f"最大并发数={config.max_concurrency}, LLM模型={processor.llm_client.client.config.model}"
        )

        tasks = [process_single_batch_extraction(i, batch) for i, batch in enumerate(batches)]
        batch_results = await asyncio.gather(*tasks, return_exceptions=False)

        all_events = []
        for batch_events in batch_results:
            all_events.extend(batch_events)

        # 计算最终统计
        final_success_rate = success_count * 100 // len(batches) if len(batches) > 0 else 0

        self.logger.info(
            f"✅ 阶段1完成 | "
            f"总批次: {len(batches)} | "
            f"成功: {success_count}✓ | "
            f"失败: {failed_count}✗ | "
            f"成功率: {final_success_rate}% | "
            f"提取事项: {len(all_events)} 个",
            extra={
                "total_batches": len(batches),
                "success_count": success_count,
                "failed_count": failed_count,
                "success_rate": final_success_rate,
                "total_sections": len(sections),
                "total_events": len(all_events),
            },
        )

        # 阶段2：统一处理所有事项的实体关联
        if all_events:
            self.logger.info("开始阶段2：统一处理实体关联")
            all_events = await processor.process_entity_associations(all_events)
            self.logger.info(f"阶段2完成，已处理 {len(all_events)} 个事项的实体关联")

        return all_events

    async def _extract_events(
        self, sections: List[SourceChunk], config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        提取事项

        Args:
            sections: 来源片段列表
            config: 提取配置

        Returns:
            提取的事项列表
        """
        # 获取LLM客户端（懒加载）
        llm_client = await self._get_llm_client()
        
        processor = EventProcessor(
            llm_client=llm_client,
            prompt_manager=self.prompt_manager,
            config=config,
        )

        # 初始化处理器（加载实体类型配置）
        await processor.initialize()

        if config.parallel:
            # 并行处理片段（智能分批）
            events = await self._process_batches_parallel(sections, processor, config)
        else:
            # 顺序处理片段
            events = []
            for i, section in enumerate(sections):
                batch_events = await processor.extract_from_sections([section], i)
                events.extend(batch_events)

        # 统一分配全局 rank（确保同一文章内的事项按顺序排列）
        for i, event in enumerate(events):
            event.rank = i

        self.logger.info(f"提取了 {len(events)} 个事项")

        return events

    async def _save_events(self, events: List[SourceEvent], config: ExtractConfig) -> None:
        """
        保存事项到数据库（包括实体关联）+ Elasticsearch

        Args:
            events: 事项列表
            config: 提取配置
        """
        self.logger.info(f"保存 {len(events)} 个事项到数据库")

        # 1. 保存到 MySQL
        event_ids = []
        async with self.session_factory() as session:
            for event in events:
                event_ids.append(event.id)  # 记录 event ID
                # 添加事项
                session.add(event)
                
                # 添加事项的所有实体关联
                if hasattr(event, 'event_associations') and event.event_associations:
                    for assoc in event.event_associations:
                        session.add(assoc)

            await session.commit()

        self.logger.info("事项保存到MySQL完成")

        # 2. 同步到 Elasticsearch（如果启用）
        if config.auto_vector:
            try:
                # 重新从数据库查询事项（带关系数据），避免 detached instance 问题
                fresh_events = await self._load_events_by_ids(event_ids)
                await self._sync_to_elasticsearch(fresh_events, config)
            except Exception as e:
                self.logger.error(f"同步到ES失败: {e}", exc_info=True)
                # 不中断流程，继续执行

    async def _load_events_by_ids(self, event_ids: List[str]) -> List[SourceEvent]:
        """
        从数据库加载事项列表（预加载关系数据）
        
        Args:
            event_ids: 事项ID列表
            
        Returns:
            事项列表
        """
        if not event_ids:
            return []
        
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEvent)
                .where(SourceEvent.id.in_(event_ids))
                .options(selectinload(SourceEvent.event_associations))
            )
            events = result.scalars().all()
            
            # 确保数据在 session 外可访问
            # 设置 expire_on_commit=False 或在这里触发加载
            session.expire_on_commit = False
            
            # 触发加载关系数据和所有字段
            for event in events:
                # 触发所有字段加载，包括 created_time 等
                _ = event.created_time
                _ = event.updated_time
                # 触发关系数据加载
                if hasattr(event, 'event_associations'):
                    _ = len(event.event_associations)
            
            return list(events)

    async def _sync_to_elasticsearch(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> None:
        """
        将事项和实体同步到Elasticsearch

        Args:
            events: 事项列表
            config: 提取配置
        """
        self.logger.info(f"开始同步 {len(events)} 个事项到Elasticsearch")
        
        # 初始化 ES 客户端（延迟初始化）
        if self.es_client is None:
            self.es_client = get_es_client()
            # Repository 需要 AsyncElasticsearch 对象，而不是 ElasticsearchClient 包装对象
            self.event_repo = EventVectorRepository(self.es_client.client)
            self.entity_repo = EntityVectorRepository(self.es_client.client)
        
        # 检查 ES 连接
        if not await self.es_client.ping():
            raise Exception("Elasticsearch 连接失败")
        
        # 收集所有唯一的实体
        unique_entities = {}  # key: entity_id, value: Entity对象
        for event in events:
            if hasattr(event, 'event_associations') and event.event_associations:
                for assoc in event.event_associations:
                    entity_id = assoc.entity_id
                    if entity_id not in unique_entities:
                        entity = await self._load_entity_by_id(entity_id)
                        if entity:
                            unique_entities[entity_id] = entity
        
        # 1. 先同步实体（因为事项会引用实体ID）
        if unique_entities:
            await self._sync_entities_to_es(list(unique_entities.values()), config)
        
        # 2. 再同步事项
        await self._sync_events_to_es(events, config)
        
        self.logger.info(
            f"ES同步完成: {len(events)} 个事项, {len(unique_entities)} 个实体"
        )


    async def _load_entity_by_id(self, entity_id: str):
        """
        从数据库加载实体

        Args:
            entity_id: 实体ID

        Returns:
            实体对象或None
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Entity).where(Entity.id == entity_id)
            )
            return result.scalar_one_or_none()

    async def _sync_entities_to_es(
        self, entities: List[Entity], config: ExtractConfig
    ) -> None:
        """
        将实体批量同步到Elasticsearch

        Args:
            entities: 实体列表
            config: 提取配置
        """
        if not entities:
            return
        
        self.logger.debug(f"同步 {len(entities)} 个实体到ES")
        
        # 逐个索引实体（因为需要生成向量）
        success_count = 0
        error_count = 0
        
        # ✅ 获取 embedding 客户端（使用 factory 配置管理）
        embedding_client = await get_embedding_client(scenario='general')
        
        for entity in entities:
            try:
                # 生成实体名称的向量（使用统一的embedding服务）
                vector = await embedding_client.generate(entity.name)
                
                # 使用 Repository 的 index_entity 方法索引
                await self.entity_repo.index_entity(
                    entity_id=entity.id,
                    source_config_id=entity.source_config_id,
                    entity_type=entity.type,
                    name=entity.name,
                    vector=vector,
                    normalized_name=entity.normalized_name or "",
                    description=entity.description or "",
                    created_time=entity.created_time.isoformat() if entity.created_time else None,
                )
                success_count += 1
                
            except Exception as e:
                self.logger.error(f"实体索引失败 {entity.id}: {e}")
                error_count += 1
        
        if error_count > 0:
            self.logger.warning(
                f"实体同步部分失败: 成功{success_count}, 失败{error_count}"
            )
        else:
            self.logger.debug(f"实体同步成功: {success_count} 个")

    async def _sync_events_to_es(
        self, events: List[SourceEvent], config: ExtractConfig
    ) -> None:
        """
        将事项批量同步到Elasticsearch

        Args:
            events: 事项列表
            config: 提取配置
        """
        if not events:
            return
        
        self.logger.debug(f"同步 {len(events)} 个事项到ES")
        
        # 逐个索引事项（因为需要生成向量）
        success_count = 0
        error_count = 0
        
        # ✅ 获取 embedding 客户端（使用 factory 配置管理）
        embedding_client = await get_embedding_client(scenario='general')
        
        for event in events:
            try:
                # 生成标题向量（使用统一的embedding服务）
                title_vector = await embedding_client.generate(event.title)
                
                # 生成内容向量（使用标题+部分内容）
                content_for_vector = f"{event.title}\n\n{event.content[:500]}"
                content_vector = await embedding_client.generate(content_for_vector)
                
                # 提取关联的实体ID列表
                entity_ids = []
                if hasattr(event, 'event_associations') and event.event_associations:
                    entity_ids = [assoc.entity_id for assoc in event.event_associations]

                # 准备额外字段
                extra_fields = {}
                if event.extra_data:
                    # category不再从extra_data读取
                    if "tags" in event.extra_data:
                        extra_fields["tags"] = event.extra_data["tags"]

                # category从独立字段读取
                if event.category:
                    extra_fields["category"] = event.category

                # 使用 Repository 的 index_event 方法索引
                await self.event_repo.index_event(
                    event_id=event.id,
                    source_config_id=event.source_config_id,
                    source_type=event.source_type,
                    source_id=event.source_id,
                    title=event.title,
                    summary=event.summary or "",
                    content=event.content,
                    title_vector=title_vector,
                    content_vector=content_vector,
                    entity_ids=entity_ids,
                    start_time=event.start_time.isoformat() if event.start_time else None,
                    end_time=event.end_time.isoformat() if event.end_time else None,
                    created_time=event.created_time.isoformat() if event.created_time else None,
                    **extra_fields,
                )
                success_count += 1
                
            except Exception as e:
                self.logger.error(f"事项索引失败 {event.id}: {e}")
                error_count += 1
        
        if error_count > 0:
            self.logger.warning(
                f"事项同步部分失败: 成功{success_count}, 失败{error_count}"
            )
        else:
            self.logger.debug(f"事项同步成功: {success_count} 个")

    async def _reload_events_with_entities(self, article_id: str) -> List[SourceEvent]:
        """
        重新加载事项，预加载实体关系
        
        Args:
            article_id: 文章ID
            
        Returns:
            包含完整实体关系的事项列表
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEvent)
                .where(SourceEvent.article_id == article_id)
                .order_by(SourceEvent.rank)
                .options(
                    selectinload(SourceEvent.event_associations).selectinload(EventEntity.entity)
                )
            )
            events = list(result.scalars().all())
        
        self.logger.debug(f"重新加载了 {len(events)} 个事项（包含实体关系）")
        return events

    # ============ 新增：基于Agent的chunk级提取 ============
    
    async def extract_from_chunk(
        self,
        chunk: SourceChunk,
        config: ExtractConfig
    ) -> List[SourceEvent]:
        """
        从chunk提取事项（使用Agent）
        
        流程：
        1. 加载chunk内容（sections或messages）+ 元数据
        2. 加载实体类型定义
        3. 创建ExtractorAgent并执行
        4. 转换结果为SourceEvent对象
        
        Args:
            chunk: 来源片段对象
            config: 提取配置
        
        Returns:
            提取的事项列表（包含chunk_id）
        """
        try:
            self.logger.info(f"开始从chunk提取: chunk_id={chunk.id}, type={chunk.source_type}")
            
            # 1. 加载内容
            content_items, metadata = await self._load_chunk_content(chunk)
            if not content_items:
                self.logger.warning(f"Chunk {chunk.id} 无内容")
                return []
            
            # 2. 加载实体类型（必传，从数据库加载）
            entity_types = await self._load_entity_types_for_chunk(config)
            
            # 3. 创建Agent并提取
            from sag.core.agent.extractor import ExtractorAgent
            
            agent = ExtractorAgent(
                chunk_type=chunk.source_type,
                model_config=self.model_config
            )
            
            result = await agent.extract(
                content_items=content_items,
                metadata=metadata,
                entity_types=entity_types,
                chunk=chunk
            )
            
            # 4. 转换为SourceEvent
            events = await self._build_events_from_result(result, chunk, config)
            
            self.logger.info(f"Chunk提取完成: chunk_id={chunk.id}, events={len(events)}")
            return events
            
        except Exception as e:
            self.logger.error(f"Chunk提取失败: {e}", exc_info=True)
            raise ExtractError(f"Chunk提取失败: {e}") from e
    
    async def _load_chunk_content(self, chunk: SourceChunk):
        """加载chunk的内容和元数据"""
        if chunk.source_type == 'ARTICLE':
            return await self._load_article_content(chunk)
        elif chunk.source_type == 'CHAT':
            return await self._load_conversation_content(chunk)
        else:
            raise ExtractError(f"不支持的类型: {chunk.source_type}")
    
    async def _load_article_content(self, chunk: SourceChunk):
        """加载文章片段 + 上文chunk内容作为背景"""
        from sag.db import Article, ArticleSection, SourceChunk as SC
        
        async with self.session_factory() as session:
            # 1. 加载文章（源背景）
            article = await session.get(Article, chunk.source_id)
            if not article:
                raise ExtractError(f"文章不存在: {chunk.source_id}")
            
            # 2. 加载当前chunk的sections（待处理内容）
            section_ids = chunk.references if chunk.references else []
            
            if section_ids:
                sections_result = await session.execute(
                    select(ArticleSection)
                    .where(ArticleSection.id.in_(section_ids))
                    .order_by(ArticleSection.rank)
                )
            else:
                sections_result = await session.execute(
                    select(ArticleSection)
                    .where(ArticleSection.article_id == chunk.source_id)
                    .order_by(ArticleSection.rank)
                )
            
            sections = list(sections_result.scalars().all())
            
            # 3. 加载上一个chunk的内容（上文背景）
            previous_chunk = None
            if chunk.rank > 0:
                prev_result = await session.execute(
                    select(SC)
                    .where(SC.source_id == chunk.source_id)
                    .where(SC.source_type == 'ARTICLE')
                    .where(SC.rank == chunk.rank - 1)
                )
                previous_chunk = prev_result.scalar_one_or_none()
            
            return sections, {
                # Article 表字段（源背景）
                "title": article.title,
                "summary": article.summary,
                "category": article.category,
                "tags": article.tags,
                
                # 当前 Chunk 信息
                "chunk_rank": chunk.rank,
                "chunk_heading": chunk.heading,
                
                # 上文 Chunk（提供上下文）
                "previous_chunk": {
                    "heading": previous_chunk.heading,
                    "content": previous_chunk.content[:800] if len(previous_chunk.content or "") > 800 else previous_chunk.content
                } if previous_chunk and previous_chunk.content else None
            }
    
    async def _load_conversation_content(self, chunk: SourceChunk):
        """加载对话消息 + 上文chunk内容作为背景"""
        from sag.db import ChatConversation, ChatMessage, SourceChunk as SC
        
        async with self.session_factory() as session:
            # 1. 加载会话（源背景）
            conversation = await session.get(ChatConversation, chunk.source_id)
            if not conversation:
                raise ExtractError(f"会话不存在: {chunk.source_id}")
            
            # 2. 加载当前chunk的messages（待处理内容）
            message_ids = chunk.references if chunk.references else []
            
            if message_ids:
                messages_result = await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.id.in_(message_ids))
                    .order_by(ChatMessage.timestamp)
                )
            else:
                messages_result = await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == chunk.source_id)
                    .order_by(ChatMessage.timestamp)
                )
            
            messages = list(messages_result.scalars().all())
            
            # 从当前messages提取参与者和时间（无需额外查询）
            participants = []
            seen_names = set()
            for msg in messages:
                if msg.sender_name and msg.sender_name not in seen_names:
                    participants.append({
                        "name": msg.sender_name,
                        "role": msg.sender_role
                    })
                    seen_names.add(msg.sender_name)
            
            time_range = ""
            if messages:
                start = messages[0].timestamp.strftime('%H:%M')
                end = messages[-1].timestamp.strftime('%H:%M')
                time_range = f"{start} ~ {end}"
            
            # 3. 加载上一个chunk的内容（上文背景）
            previous_chunk = None
            if chunk.rank > 0:
                prev_result = await session.execute(
                    select(SC)
                    .where(SC.source_id == chunk.source_id)
                    .where(SC.source_type == 'CHAT')
                    .where(SC.rank == chunk.rank - 1)
                )
                previous_chunk = prev_result.scalar_one_or_none()
            
            return messages, {
                # ChatConversation 表字段（源背景）
                "title": conversation.title,
                "messages_count": conversation.messages_count,
                
                # extra_data 字段
                "platform": conversation.extra_data.get("platform") if conversation.extra_data else None,
                "scenario": conversation.extra_data.get("scenario") if conversation.extra_data else None,
                
                # 从当前messages提取
                "participants": participants,
                "time_range": time_range,
                
                # 当前 Chunk 信息
                "chunk_rank": chunk.rank,
                "chunk_heading": chunk.heading,
                
                # 上文 Chunk（提供上下文）
                "previous_chunk": {
                    "heading": previous_chunk.heading,
                    "content": previous_chunk.content[:800] if len(previous_chunk.content or "") > 800 else previous_chunk.content
                } if previous_chunk and previous_chunk.content else None
            }
    
    async def _load_entity_types_for_chunk(self, config: ExtractConfig) -> List[Dict]:
        """
        加载实体类型定义（必定返回非空列表）
        
        优先级：
        1. 默认全局类型（is_default=True）
        2. source级别自定义类型
        3. config中的运行时类型
        """
        entity_types = []
        
        async with self.session_factory() as session:
            # 加载默认类型
            default_result = await session.execute(
                select(DBEntityType)
                .where(DBEntityType.is_default == True)
                .where(DBEntityType.is_active == True)
            )
            default_types = default_result.scalars().all()
            
            entity_types.extend([{
                "type": et.type,
                "name": et.name,
                "description": et.description or "",
                "weight": float(et.weight)
            } for et in default_types])
            
            # 加载source级别类型
            if config.source_config_id:
                custom_result = await session.execute(
                    select(DBEntityType)
                    .where(DBEntityType.source_config_id == config.source_config_id)
                    .where(DBEntityType.is_active == True)
                )
                custom_types = custom_result.scalars().all()
                
                entity_types.extend([{
                    "type": et.type,
                    "name": et.name,
                    "description": et.description or "",
                    "weight": float(et.weight)
                } for et in custom_types])
        
        # 运行时类型（最高优先级）
        if config.custom_entity_types:
            entity_types.extend([{
                "type": et.type,
                "name": et.name,
                "description": et.description,
                "weight": et.weight
            } for et in config.custom_entity_types])
        
        return entity_types
    
    async def _build_events_from_result(
        self,
        result: Dict,
        chunk: SourceChunk,
        config: ExtractConfig
    ) -> List[SourceEvent]:
        """转换Agent结果为SourceEvent"""
        events = []
        
        for idx, evt in enumerate(result.get('events', [])):
            # 处理 references 格式（兼容旧格式和新格式）
            raw_references = evt.get('references', [])
            if raw_references and isinstance(raw_references[0], dict):
                # 旧格式: [{"type": "section", "id": "xxx"}] -> 提取 ID
                references = [ref['id'] for ref in raw_references if isinstance(ref, dict) and 'id' in ref]
            else:
                # 新格式: ["xxx", "yyy"] 或空列表
                references = raw_references
            
            # 🆕 根据来源类型设置时间
            from datetime import datetime
            from sag.db import ChatMessage
            from sqlalchemy import select as sql_select
            
            start_time = None
            end_time = None
            
            if chunk.source_type == "ARTICLE":
                # 文档类型：使用当前时间
                current_time = datetime.now()
                start_time = current_time
                end_time = current_time
                
            elif chunk.source_type == "CHAT":
                # 会话类型：从事项引用的消息中获取时间范围
                # ✅ 使用事项自己的 references（从 LLM 提取结果来的）
                if references and isinstance(references, list):
                    async with self.session_factory() as session:
                        result_msgs = await session.execute(
                            sql_select(ChatMessage)
                            .where(ChatMessage.id.in_(references))
                            .order_by(ChatMessage.timestamp)
                        )
                        messages = list(result_msgs.scalars().all())
                        
                        if messages:
                            start_time = messages[0].timestamp
                            end_time = messages[-1].timestamp
            
            # 创建事项
            event = SourceEvent(
                id=str(uuid.uuid4()),
                source_config_id=config.source_config_id,
                source_type=chunk.source_type,
                source_id=chunk.source_id,
                article_id=chunk.source_id if chunk.source_type == 'ARTICLE' else None,
                conversation_id=chunk.source_id if chunk.source_type == 'CHAT' else None,
                chunk_id=chunk.id,
                title=evt['title'],
                summary=evt['summary'],
                content=evt['content'],
                category=evt.get('category', ''),
                # 业务字段（兼容主系统）- type与source_type保持一致
                type=chunk.source_type,
                priority="UNKNOWN",  # 默认值
                status="UNKNOWN",  # 默认值
                rank=idx,
                start_time=start_time,  # 🆕
                end_time=end_time,      # 🆕
                references=references
            )
            
            # 关联实体
            event.event_associations = []
            
            for entity_data in evt.get('entities', []):
                entity = await self._get_or_create_entity(entity_data, config)
                
                # ✅ 跳过无效实体（type不存在时返回None）
                if entity is None:
                    continue
                
                assoc = EventEntity(
                    id=str(uuid.uuid4()),
                    event_id=event.id,
                    entity_id=entity.id,
                    description=entity_data.get('description', '')
                )
                # ✅ 不设置 entity 关系，避免跨 session 冲突
                # assoc.entity = entity  # 移除，会在保存后重新加载
                
                event.event_associations.append(assoc)
            
            events.append(event)
        
        return events
    
    async def _get_or_create_entity(
        self,
        entity_data: Dict,
        config: ExtractConfig
    ) -> Optional[Entity]:
        """
        查找或创建实体（去重）
        
        Returns:
            Entity对象，如果实体类型无效则返回None
        """
        normalized_name = entity_data['name'].strip().lower()
        
        async with self.session_factory() as session:
            # 查找已存在
            existing_result = await session.execute(
                select(Entity)
                .where(Entity.source_config_id == config.source_config_id)
                .where(Entity.type == entity_data['type'])
                .where(Entity.normalized_name == normalized_name)
            )
            entity = existing_result.scalar_one_or_none()
            
            if entity:
                return entity
            
            # 创建新实体 - 获取entity_type_id
            entity_type_result = await session.execute(
                select(DBEntityType)
                .where(DBEntityType.type == entity_data['type'])
                .where(
                    (DBEntityType.source_config_id == config.source_config_id) |
                    (DBEntityType.is_default == True)
                )
                .where(DBEntityType.is_active == True)
            )
            entity_type = entity_type_result.scalar_one_or_none()
            
            if not entity_type:
                # ✅ 记录警告但不抛异常，返回 None
                self.logger.warning(
                    f"跳过无效实体类型: type={entity_data['type']}, "
                    f"name={entity_data.get('name', 'N/A')}"
                )
                return None
            
            entity = Entity(
                id=str(uuid.uuid4()),
                source_config_id=config.source_config_id,
                entity_type_id=entity_type.id,
                type=entity_data['type'],
                name=entity_data['name'],
                normalized_name=normalized_name,
                description=entity_data.get('description', '')
            )
            
            session.add(entity)
            await session.commit()
            await session.refresh(entity)
            
            return entity
    
    async def _reload_events_with_relations(self, event_ids: List[str]) -> List[SourceEvent]:
        """
        重新从数据库加载事项列表（预加载关系数据）
        
        解决跨 session 问题：保存后重新查询，确保所有关系正确加载
        
        Args:
            event_ids: 事项ID列表
            
        Returns:
            包含完整关系的事项列表
        """
        if not event_ids:
            return []
        
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEvent)
                .where(SourceEvent.id.in_(event_ids))
                .options(
                    selectinload(SourceEvent.event_associations).selectinload(EventEntity.entity)
                )
            )
            events = list(result.scalars().all())
            
            # 设置 expire_on_commit=False，确保数据在 session 外可访问
            session.expire_on_commit = False
            
            # 触发关系数据加载（确保所有字段在 session 外可访问）
            for event in events:
                # 触发事项字段加载
                _ = event.title
                _ = event.created_time
                
                # 触发关联和实体加载
                if hasattr(event, 'event_associations'):
                    for assoc in event.event_associations:
                        _ = assoc.id
                        if assoc.entity:
                            _ = assoc.entity.name
                            _ = assoc.entity.type
            
            self.logger.debug(f"重新加载了 {len(events)} 个事项（包含完整关系）")
            return events
    
    async def _update_source_status(
        self,
        chunks: List[SourceChunk],
        status: str,
        error: Optional[str] = None
    ) -> None:
        """
        更新源状态（Article 或 ChatConversation）
        
        Args:
            chunks: chunk列表（用于确定源类型和ID）
            status: 状态值（PENDING/PROCESSING/COMPLETED/FAILED）
            error: 错误信息（可选，失败时提供）
        """
        if not chunks:
            return
        
        from sag.db import Article, ChatConversation
        
        # 确定源类型和ID（同一批chunks应该来自同一个源）
        source_type = chunks[0].source_type
        source_id = chunks[0].source_id
        
        # 验证所有chunks来自同一个源
        if not all(c.source_type == source_type and c.source_id == source_id for c in chunks):
            self.logger.warning("Chunks来自不同的源，无法统一更新状态")
            return
        
        async with self.session_factory() as session:
            try:
                if source_type == "ARTICLE":
                    result = await session.execute(
                        select(Article).where(Article.id == source_id)
                    )
                    source = result.scalar_one_or_none()
                    
                    if source:
                        source.status = status
                        if error:
                            source.error = error
                        await session.commit()
                        self.logger.info(f"✅ 已更新文章状态: {source_id} -> {status}")
                    else:
                        self.logger.warning(f"文章不存在: {source_id}")
                
                elif source_type == "CHAT":
                    result = await session.execute(
                        select(ChatConversation).where(ChatConversation.id == source_id)
                    )
                    source = result.scalar_one_or_none()
                    
                    if source:
                        # ChatConversation 没有 status 字段，记录到 extra_data
                        if source.extra_data is None:
                            source.extra_data = {}
                        source.extra_data["extract_status"] = status
                        if error:
                            source.extra_data["extract_error"] = error
                        await session.commit()
                        self.logger.info(f"✅ 已更新会话提取状态: {source_id} -> {status}")
                    else:
                        self.logger.warning(f"会话不存在: {source_id}")
                
                else:
                    self.logger.warning(f"不支持的源类型: {source_type}")
            
            except Exception as e:
                self.logger.error(f"更新源状态失败: {e}", exc_info=True)
                # 不抛出异常，避免影响主流程
    