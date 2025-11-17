"""
事项处理器

负责从文章片段中提取事项和实体的核心逻辑
"""

import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select

from sag.core.ai.base import BaseLLMClient
from sag.core.ai.models import LLMMessage, LLMRole
from sag.core.prompt.manager import PromptManager
from sag.db import get_session_factory
from sag.db.models import (
    SourceChunk,
    Entity,
    EntityType as DBEntityType,
    EventEntity,
    SourceEvent,
)
from sag.exceptions import ExtractError
from sag.modules.extract.config import ExtractConfig
from sag.modules.extract.parser import EntityValueParser
from sag.utils import get_logger

logger = get_logger("extract.processor")


class EventProcessor:
    """事项处理器（核心提取逻辑）"""

    def __init__(
        self,
        llm_client: BaseLLMClient,
        prompt_manager: PromptManager,
        config: ExtractConfig,
    ):
        """
        初始化事项处理器

        Args:
            llm_client: LLM客户端
            prompt_manager: 提示词管理器
            config: 提取配置
        """
        self.llm_client = llm_client
        self.prompt_manager = prompt_manager
        self.config = config
        self.session_factory = get_session_factory()
        self.entity_types: List[DBEntityType] = []
        self.logger = get_logger("extract.processor")
        self.parser = EntityValueParser()  # 🆕 初始化值解析器

    async def extract_from_sections(
        self, sections: List[SourceChunk], batch_index: int
    ) -> List[SourceEvent]:
        """
        从来源片段提取事项（核心方法）

        这是最底层的提取逻辑，单次LLM调用

        Args:
            sections: 来源片段列表
            batch_index: 批次索引（用于日志）

        Returns:
            提取的事项列表

        Raises:
            ExtractError: 提取失败
        """
        # 输入验证
        if not sections:
            self.logger.warning(f"批次 {batch_index}: sections 列表为空，跳过提取")
            return []

        try:
            # 1. 构建上下文
            context = self._build_context(sections)

            # 2. 构建提示词
            prompt = self._build_prompt(context)
            self.logger.info(f"提示词:: {prompt}")

            # 3. 构建JSON Schema
            schema = self._build_extraction_schema()

            # 4. 调用LLM
            messages = [LLMMessage(role=LLMRole.USER, content=prompt)]

            result = await self.llm_client.chat_with_schema(
                messages, response_schema=schema, temperature=0.3
            )

            # 5. 解析结果 -> SourceEvent 对象
            events = await self._parse_extraction_result(result, sections)

            self.logger.info(
                f"批次 {batch_index}: 提取了 {len(events)} 个事项",
                extra={"batch_index": batch_index, "event_count": len(events)},
            )

            return events

        except Exception as e:
            self.logger.error(f"批次 {batch_index} 提取失败: {e}", exc_info=True)
            raise ExtractError(f"批次 {batch_index} 提���失败: {e}") from e

    async def extract_events_without_entities(
        self, sections: List[SourceChunk], batch_index: int
    ) -> List[SourceEvent]:
        """
        阶段1：提取事项（不含实体关联）

        Args:
            sections: 来源片段列表
            batch_index: 批次索引

        Returns:
            不含实体关联的事项列表
        """
        try:
            # 1. 构建上下文
            context = self._build_context(sections)

            # 2. 构建提示词
            prompt = self._build_prompt(context)

            # 3. 构建JSON Schema
            schema = self._build_extraction_schema()

            # 4. 调用LLM
            messages = [LLMMessage(role=LLMRole.USER, content=prompt)]

            self.logger.info(
                f"📦 批次 {batch_index}: 开始提取事项（不含实体） - 片段数={len(sections)}, "
                f"LLM模型={self.llm_client.client.config.model}"
            )

            result = await self.llm_client.chat_with_schema(
                messages, response_schema=schema, temperature=0.3
            )

            # 5. 解析结果（不处理实体关联）
            events = await self._parse_extraction_result_without_entities(result, sections)

            self.logger.info(
                f"批次 {batch_index}: 提取了 {len(events)} 个事项（不含实体）",
                extra={"batch_index": batch_index, "event_count": len(events)},
            )

            return events

        except Exception as e:
            self.logger.error(
                f"❌ 批次 {batch_index} 提取失败 - 模型: {self.llm_client.client.config.model}, "
                f"片段数: {len(sections)}, 错误: {e}",
                exc_info=True
            )
            raise ExtractError(f"批次 {batch_index} 提取失败: {e}") from e

    async def process_entity_associations(
        self, events: List[SourceEvent], session=None
    ) -> List[SourceEvent]:
        """
        阶段2：统一处理所有事项的实体关联（带 session 支持）

        Args:
            events: 所有事项列表（不含实体关联）
            session: 数据库 session（可选，如果提供则使用该 session）

        Returns:
            包含实体关联的事项列表
        """
        try:
            self.logger.info(f"开始统一处理 {len(events)} 个事项的实体关联")

            # 收集所有实体数据（包括 LLM 提取的 + 默认值实体）
            # 使用字典存储：key=entity_name, value=description
            all_entities_data = {}

            # 1️⃣ 先收集 LLM 提取的实体
            for event in events:
                entities_data = event.extra_data.get("raw_entities", {})
                for entity_type, entity_names in entities_data.items():
                    if entity_type not in all_entities_data:
                        all_entities_data[entity_type] = {}  # 改为字典

                    # 兼容新旧格式
                    for entity_data in entity_names:
                        if isinstance(entity_data, dict):
                            name = entity_data.get("name")
                            description = entity_data.get("description", "")
                        else:
                            # 旧格式：直接是字符串
                            name = entity_data
                            description = ""

                        if name:
                            # 如果已存在且没有描述，用新的描述更新
                            if name not in all_entities_data[entity_type]:
                                all_entities_data[entity_type][name] = description
                            elif description and not all_entities_data[entity_type][name]:
                                all_entities_data[entity_type][name] = description

            # 2️⃣ 添加配置的默认值实体到收集池
            for entity_type_config in self.entity_types:
                constraints = entity_type_config.value_constraints or {}
                default_value = constraints.get('default')
                if default_value:
                    entity_type = entity_type_config.type
                    if entity_type not in all_entities_data:
                        all_entities_data[entity_type] = {}
                    # 添加默认值实体（如果还没有）
                    if default_value not in all_entities_data[entity_type]:
                        all_entities_data[entity_type][default_value] = "系统默认值"
                        self.logger.debug(
                            f"📌 添加默认值实体到收集池: {entity_type}={default_value}"
                        )

            # 实体缓存，避免重复查询
            # key: (entity_type, normalized_name), value: entity_id
            entity_id_map = {}

            # 判断是否需要创建新 session
            should_close_session = False
            if session is None:
                session = self.session_factory()
                session = await session.__aenter__()
                should_close_session = True

            try:
                # 统一创建/获取所有实体（使用同一个 session）
                for entity_type, entities_dict in all_entities_data.items():
                    entity_type_obj = self._get_entity_type_by_type(
                        entity_type)
                    if not entity_type_obj:
                        continue

                    for name, description in entities_dict.items():
                        normalized_name = self._normalize_entity_name(name)
                        cache_key = (entity_type, normalized_name)

                        # 检查缓存
                        if cache_key in entity_id_map:
                            continue

                        # 获取或创建实体ID（不传入description）
                        entity_id = await self._get_or_create_entity_with_session(
                            session, entity_type, name, normalized_name, entity_type_obj
                        )
                        # 在缓存中同时存储ID和description
                        entity_id_map[cache_key] = (entity_id, description)

                # 如果创建了新 session，需要提交实体的创建
                if should_close_session:
                    await session.commit()
                    self.logger.debug(f"已提交 {len(entity_id_map)} 个实体到数据库")

                # 📌 预先收集所有强制模式的默认值（用于标记）
                forced_defaults = {}  # {entity_type: default_value}
                for entity_type_config in self.entity_types:
                    constraints = entity_type_config.value_constraints or {}
                    default_value = constraints.get('default')
                    override_mode = constraints.get('override', False)
                    if default_value and override_mode:
                        forced_defaults[entity_type_config.type] = default_value

                # 为所有事项建立实体关联
                for event in events:
                    entities_data = event.extra_data.get("raw_entities", {})
                    event_associations = []
                    
                    # 🆕 使用字典跟踪每个实体ID及其信息（防止重复关联）
                    entity_map = {}  # {entity_id: {"name": str, "descriptions": [str], "weight": float, ...}}

                    # 3️⃣ 建立 LLM 提取的实体关联
                    for entity_type, entity_names in entities_data.items():
                        entity_type_obj = self._get_entity_type_by_type(
                            entity_type)
                        if not entity_type_obj:
                            continue

                        for entity_data in entity_names:
                            # 兼容新旧格式
                            if isinstance(entity_data, dict):
                                name = entity_data.get("name")
                                description = entity_data.get(
                                    "description", "")
                            else:
                                name = entity_data
                                description = ""

                            if not name:
                                continue

                            normalized_name = self._normalize_entity_name(name)
                            key = (entity_type, normalized_name)
                            if key in entity_id_map:
                                # 从缓存获取entity_id和description
                                entity_id, cached_description = entity_id_map[key]
                                
                                # 🆕 检查是否已添加过这个实体
                                if entity_id not in entity_map:
                                    # 首次添加
                                    entity_map[entity_id] = {
                                        "name": name,
                                        "type": entity_type,
                                        "descriptions": [],
                                        "weight": float(entity_type_obj.weight),
                                        "is_forced_default": False
                                    }
                                
                                # 收集描述
                                if description and description not in entity_map[entity_id]["descriptions"]:
                                    entity_map[entity_id]["descriptions"].append(description)
                                if cached_description and cached_description not in entity_map[entity_id]["descriptions"]:
                                    entity_map[entity_id]["descriptions"].append(cached_description)
                                
                                # 检查是否是强制模式的默认值
                                if entity_type in forced_defaults and name == forced_defaults[entity_type]:
                                    entity_map[entity_id]["is_forced_default"] = True

                    # 4️⃣ 应用默认值实体关联逻辑
                    extracted_by_type = {}
                    for entity_type, entity_names in entities_data.items():
                        names = []
                        for e in entity_names:
                            name = e.get('name') if isinstance(e, dict) else e
                            if name:
                                names.append(name)
                        extracted_by_type[entity_type] = names

                    # 检查每个实体类型的默认值配置
                    for entity_type_config in self.entity_types:
                        constraints = entity_type_config.value_constraints or {}
                        default_value = constraints.get('default')
                        override_mode = constraints.get('override', False)

                        if not default_value:
                            continue

                        entity_type = entity_type_config.type
                        entity_names_of_type = extracted_by_type.get(
                            entity_type, [])
                        has_default = default_value in entity_names_of_type

                        # 判断是否需要添加默认值关联
                        should_add_default = False
                        if override_mode:
                            # 强制模式：总是要有（但如果LLM已提取就不重复）
                            should_add_default = not has_default
                        else:
                            # 补充模式：仅当该类型完全没有实体时补充
                            should_add_default = len(entity_names_of_type) == 0

                        if should_add_default:
                            # 从缓存获取默认值实体ID
                            normalized_name = self._normalize_entity_name(
                                default_value)
                            key = (entity_type, normalized_name)
                            if key in entity_id_map:
                                entity_id, _ = entity_id_map[key]
                                
                                # 🆕 检查是否已添加过这个实体
                                if entity_id not in entity_map:
                                    mode_desc = "强制追加" if override_mode else "自动补充"
                                    
                                    entity_map[entity_id] = {
                                        "name": default_value,
                                        "type": entity_type,
                                        "descriptions": [f"系统默认值（{mode_desc}）"],
                                        "weight": float(entity_type_config.weight),
                                        "is_forced_default": False,
                                        "is_default": True,
                                        "mode": mode_desc
                                    }
                                    
                                    self.logger.debug(
                                        f"✅ {mode_desc}默认值关联: {entity_type}={default_value}, "
                                        f"event_id={event.id[:8]}..."
                                    )
                                else:
                                    self.logger.debug(
                                        f"⏭️  跳过默认值（已存在）: {entity_type}={default_value}, "
                                        f"event_id={event.id[:8]}..."
                                    )

                    # 🆕 为每个唯一的 entity_id 创建一个关联（合并描述）
                    for entity_id, info in entity_map.items():
                        # 合并描述
                        if info.get("is_forced_default"):
                            final_description = "系统默认值（强制写入）"
                        elif info.get("is_default"):
                            final_description = info["descriptions"][0] if info["descriptions"] else None
                        elif info["descriptions"]:
                            final_description = "、".join(info["descriptions"])
                        else:
                            final_description = None
                        
                        # 创建关联
                        extra_data = {
                            "confidence": event.extra_data.get("quality_score", 0.8),
                        }
                        if info.get("is_forced_default"):
                            extra_data["is_forced_default"] = True
                        if info.get("is_default"):
                            extra_data["is_default"] = True
                            extra_data["mode"] = info.get("mode")
                        if len(info["descriptions"]) > 1:
                            extra_data["description_count"] = len(info["descriptions"])
                        
                        assoc = EventEntity(
                            id=str(uuid.uuid4()),
                            event_id=event.id,
                            entity_id=entity_id,
                            weight=info["weight"],
                            description=final_description,
                            extra_data=extra_data,
                        )
                        event_associations.append(assoc)
                        
                        # 日志：合并了多个描述
                        if len(info["descriptions"]) > 1:
                            self.logger.debug(
                                f"✅ 合并实体描述: {info['name']} ({len(info['descriptions'])}个) -> {final_description}"
                            )

                    event.event_associations = event_associations

                    # 清理临时数据
                    if "raw_entities" in event.extra_data:
                        del event.extra_data["raw_entities"]

                self.logger.info(f"完成 {len(events)} 个事项的实体关联处理")
                return events

            finally:
                if should_close_session:
                    await session.__aexit__(None, None, None)

        except Exception as e:
            self.logger.error(f"实体关联处理失败: {e}", exc_info=True)
            raise ExtractError(f"实体关联处理失败: {e}") from e

    async def _parse_extraction_result_without_entities(
        self, result: Dict[str, Any], sections: List[SourceChunk]
    ) -> List[SourceEvent]:
        """
        解析LLM提取结果为SourceEvent对象（不处理实体关联）

        Args:
            result: LLM返回的JSON结果
            sections: 原始片段列表（用于生成引用）

        Returns:
            不含实体关联的SourceEvent对象列表
        """
        events = []
        for event_data in result.get("events", []):
            # 解析 LLM 标注的引用（片段编号，从1开始）
            referenced_indices = event_data.get("references", [])
            # 将片段编号转换为实际的 section_id
            referenced_section_ids = []
            invalid_indices = []
            for idx in referenced_indices:
                if isinstance(idx, int) and 1 <= idx <= len(sections):  # 验证索引有效性
                    section = sections[idx - 1]  # 编号从1开始，索引从0开始
                    referenced_section_ids.append(section.id)
                else:
                    # 记录无效索引
                    invalid_indices.append(idx)

            # 记录警告（如果有无效索引）
            if invalid_indices:
                self.logger.warning(
                    f"事项 '{event_data.get('title', '未知')}' 包含无效的片段引用索引: {invalid_indices}",
                    extra={
                        "event_title": event_data.get("title"),
                        "invalid_indices": invalid_indices,
                        "total_sections": len(sections),
                    },
                )

            # 🆕 ==================== 实体转换、去重与合并逻辑（源头处理）====================
            # 1. 将 LLM 返回的数组格式转换为按 type 分组的字典格式
            entities_from_llm = event_data.get("entities", [])
            entities_raw = {}

            # 如果 LLM 返回的是数组（schema 定义的格式）
            if isinstance(entities_from_llm, list):
                for entity_item in entities_from_llm:
                    if not isinstance(entity_item, dict):
                        continue
                    
                    entity_type = entity_item.get("type")
                    if not entity_type:
                        continue
                    
                    # 按类型分组
                    if entity_type not in entities_raw:
                        entities_raw[entity_type] = []
                    
                    entities_raw[entity_type].append({
                        "name": entity_item.get("name", ""),
                        "description": entity_item.get("description", "")  # 保留 description
                    })
            # 兼容旧的字典格式（如果存在）
            elif isinstance(entities_from_llm, dict):
                entities_raw = entities_from_llm

            # 2. 对每个类型内的实体去重，并智能合并 description
            entities_deduped = {}

            for entity_type, entity_list in entities_raw.items():
                if not entity_list:
                    entities_deduped[entity_type] = []
                    continue
                
                # 使用字典收集：key=normalized_name, value={"name": str, "descriptions": [str]}
                merged_entities = {}
                
                for entity_data in entity_list:
                    # 兼容格式：字典或字符串
                    if isinstance(entity_data, dict):
                        name = entity_data.get("name", "").strip()
                        description = entity_data.get("description", "").strip()
                    else:
                        name = str(entity_data).strip()
                        description = ""
                    
                    if not name:
                        continue
                    
                    # 规范化名称用于去重
                    normalized_name = name.lower().strip()
                    
                    # 第一次遇到这个实体
                    if normalized_name not in merged_entities:
                        merged_entities[normalized_name] = {
                            "name": name,  # 保留原始名称（第一次出现的）
                            "descriptions": []
                        }
                    
                    # 收集描述（去重、去空）
                    if description:
                        existing_descs = merged_entities[normalized_name]["descriptions"]
                        if description not in existing_descs:
                            existing_descs.append(description)
                
                # 转换回列表格式，合并描述
                deduped_list = []
                for entity_info in merged_entities.values():
                    # 用中文顿号连接多个描述
                    final_desc = "、".join(entity_info["descriptions"]) if entity_info["descriptions"] else ""
                    
                    deduped_list.append({
                        "name": entity_info["name"],
                        "description": final_desc  # 合并后的描述
                    })
                    
                    if len(entity_info["descriptions"]) > 1:
                        self.logger.debug(
                            f"✅ 合并重复实体描述 [{entity_type}] {entity_info['name']}: "
                            f"{len(entity_info['descriptions'])}个 -> {final_desc}"
                        )
                
                entities_deduped[entity_type] = deduped_list
            # =================================================================

            # 确定主要引用的 chunk（取第一个被引用的 chunk）
            primary_chunk = None
            if referenced_section_ids:
                # 查找第一个被引用的 section 对应的 chunk
                for section in sections:
                    if section.id == referenced_section_ids[0]:
                        primary_chunk = section
                        break
                if not primary_chunk:
                    primary_chunk = sections[0]  # 如果没找到，默认使用第一个 chunk
            else:
                primary_chunk = sections[0] if sections else None

            # 🆕 根据来源类型设置时间
            # 注意：在 processor 中，事项的 references 直接继承自 primary_chunk.references
            # 所以用 primary_chunk.references 来查询时间是正确的
            from datetime import datetime
            from sag.db import ChatMessage
            from sqlalchemy import select
            
            start_time = None
            end_time = None
            event_references = primary_chunk.references if primary_chunk else None
            
            if primary_chunk:
                if primary_chunk.source_type == "ARTICLE":
                    # 文档类型：使用当前时间
                    current_time = datetime.now()
                    start_time = current_time
                    end_time = current_time
                    
                elif primary_chunk.source_type == "CHAT":
                    # 会话类型：从引用的消息中获取时间范围
                    # 使用 primary_chunk.references（因为事项会继承这个）
                    if event_references and isinstance(event_references, list):
                        async with self.session_factory() as session:
                            result_msgs = await session.execute(
                                select(ChatMessage)
                                .where(ChatMessage.id.in_(event_references))
                                .order_by(ChatMessage.timestamp)
                            )
                            messages = list(result_msgs.scalars().all())
                            
                            if messages:
                                start_time = messages[0].timestamp  # 最早时间
                                end_time = messages[-1].timestamp  # 最晚时间
                                self.logger.debug(
                                    f"会话事项时间: {start_time} ~ {end_time} "
                                    f"(共{len(messages)}条消息)"
                                )

            # 创建事项对象
            source_type_value = primary_chunk.source_type if primary_chunk else "ARTICLE"
            event = SourceEvent(
                id=str(uuid.uuid4()),
                source_config_id=self.config.source_config_id,
                source_type=source_type_value,
                source_id=primary_chunk.source_id if primary_chunk else sections[0].source_id,
                article_id=sections[0].article_id if primary_chunk and primary_chunk.source_type == "ARTICLE" else None,
                conversation_id=primary_chunk.conversation_id if primary_chunk and primary_chunk.source_type == "CHAT" else None,
                title=event_data["title"],
                summary=event_data.get("summary") or "",
                content=event_data["content"],
                category=event_data.get("category") or "",  # 独立字段，确保None转为空字符串
                # 业务字段（兼容主系统）- type与source_type保持一致
                type=source_type_value,
                priority="UNKNOWN",  # 默认值
                status="UNKNOWN",  # 默认值
                rank=None,  # 由上层 EventExtractor 统一分配全局 rank
                start_time=start_time,
                end_time=end_time,
                references=referenced_section_ids,  # ✅ 修复：使用LLM精确标注的引用
                chunk_id=primary_chunk.id if primary_chunk else None,
                extra_data={
                    "quality_score": event_data.get("quality_score", 0.8),
                    "batch_size": len(sections),
                    # 保存去重后的实体数据，用于第二阶段处理
                    "raw_entities": entities_deduped,
                },
            )

            events.append(event)

        return events

    async def initialize(self) -> None:
        """
        初始化处理器（加载实体类型配置）

        必须在使用处理器之前调用此方法
        """
        await self._load_entity_types()

    async def _load_entity_types(self) -> None:
        """
        从数据库加载实体类型配置

        加载规则（按优先级从高到低）：
        1. 文档级别（scope='article', article_id=当前文档）
        2. 信息源级别（scope='source', source_config_id=当前信息源）
        3. 全局自定义（scope='global', source_config_id IS NULL, is_default=FALSE）
        4. 系统默认（source_config_id IS NULL, is_default=TRUE）

        注意：同一个 type 只取优先级最高的配置
        """
        async with self.session_factory() as session:
            # 查询条件列表（按优先级排序）
            conditions = []

            # 1. 文档级别（优先级最高）
            if self.config.article_id:
                conditions.append(
                    (DBEntityType.scope == 'article')
                    & (DBEntityType.article_id == self.config.article_id)
                    & DBEntityType.is_active
                )

            # 2. 信息源级别
            if self.config.source_config_id:
                conditions.append(
                    (DBEntityType.scope == 'source')
                    & (DBEntityType.source_config_id == self.config.source_config_id)
                    & DBEntityType.is_active
                )

            # 3. 全局自定义类型
            conditions.append(
                (DBEntityType.scope == 'global')
                & DBEntityType.source_config_id.is_(None)
                & (DBEntityType.is_default == False)
                & DBEntityType.is_active
            )

            # 4. 系统默认类型
            conditions.append(
                DBEntityType.source_config_id.is_(None)
                & DBEntityType.is_default
                & DBEntityType.is_active
                    )

            # 查询所有匹配的实体类型
            result = await session.execute(
                select(DBEntityType)
                .where(or_(*conditions))
                .order_by(DBEntityType.weight.desc())
            )
            all_entity_types = list(result.scalars().all())

            # 去重：同一个 type 只保留优先级最高的
            # 优先级：文档 > 信息源 > 全局 > 默认
            type_priority_map = {}
            for et in all_entity_types:
                if et.type not in type_priority_map:
                    # 第一次出现该类型，记录下来
                    type_priority_map[et.type] = et
                else:
                    # 该类型已存在，比较优先级
                    existing = type_priority_map[et.type]

                    # 确定优先级得分（数值越小优先级越高）
                    def get_priority_score(entity_type):
                        if entity_type.scope == 'article' and entity_type.article_id == self.config.article_id:
                            return 1  # 文档级别
                        elif entity_type.scope == 'source' and entity_type.source_config_id == self.config.source_config_id:
                            return 2  # 信息源级别
                        elif entity_type.scope == 'global' and not entity_type.is_default:
                            return 3  # 全局自定义
                        elif entity_type.is_default:
                            return 4  # 系统默认
                        else:
                            return 5  # 其他（不应该出现）

                    if get_priority_score(et) < get_priority_score(existing):
                        type_priority_map[et.type] = et

            self.entity_types = list(type_priority_map.values())

        self.logger.info(
            f"加载了 {len(self.entity_types)} 个实体类型配置",
            extra={
                "article_id": self.config.article_id,
                "source_config_id": self.config.source_config_id,
                "entity_types": [et.type for et in self.entity_types],
            },
        )

        # 🔍 调试：输出每个实体类型的详细信息
        for et in self.entity_types:
            scope_desc = f"{et.scope}"
            if et.scope == 'article':
                scope_desc += f"(article_id={et.article_id[:8]}...)"
            elif et.scope == 'source':
                scope_desc += f"(source_config_id={et.source_config_id[:8] if et.source_config_id else 'None'}...)"
            elif et.is_default:
                scope_desc += "(default)"

            self.logger.info(
                f"🔍 实体类型 [{et.type}]: "
                f"name={et.name}, scope={scope_desc}, "
                f"is_active={et.is_active}, is_default={et.is_default}, "
                f"value_constraints={et.value_constraints}"
            )

    def _build_context(self, sections: List[SourceChunk]) -> str:
        """
        构建来源片段上下文

        Args:
            sections: 来源片段列表

        Returns:
            格式化的上下文文本
        """
        context_parts = []

        for i, section in enumerate(sections, 1):
            context_parts.append(f"## 片段 {i}: {section.heading}")
            context_parts.append(f"{section.content}")
            context_parts.append("")  # 空行分隔

        return "\n".join(context_parts)

    def _build_prompt(self, context: str) -> str:
        """
        构建提示词

        Args:
            context: 上下文文本

        Returns:
            完整的提示词
        """
        # 获取实体类型说明
        entity_types_desc = self._get_entity_types_description()

        # 使用PromptManager渲染模板
        try:
            prompt = self.prompt_manager.render(
                "event_extraction",
                context=context,
                background=self.config.background or "",
                entity_types=entity_types_desc,
            )
        except Exception as e:
            # 如果模板不存在，使用内置模板
            self.logger.warning(f"提示词模板不存在，使用内置模板: {e}")
            prompt = self._build_default_prompt(context, entity_types_desc)

        return prompt

    def _build_default_prompt(self, context: str, entity_types_desc: str) -> str:
        """构建默认提示词（当YAML模板不存在时）"""
        background_section = (
            f"\n## 背景信息\n{self.config.background}\n" if self.config.background else ""
        )

        return f"""你是一个专业的信息提取助手。请从以下文章片段中提取事项（Events）和实体（Entities）。
            {background_section}
            ## 提取规则

            ### 事项（Event）
            - 独立的、完整的信息单元
            - 可以是：事件、会议、决策、发现、结论、任务等
            - 每个事项必须包含标题、内容
            - **重要**：必须标注该事项引用了哪些片段（填写片段编号，如 [1, 2]）
            - 为每个事项评估质量分数（0-1，越高表示信息越完整、越有价值）

            ### 实体（Entity）
            按以下维度提取实体：

            {entity_types_desc}

            ## 文章片段

            {context}

            ## 输出要求
            严格按照JSON Schema格式返回，不要包含任何其他文本。
        """

    def _get_entity_types_description(self) -> str:
        """获取实体类型说明"""
        lines = []

        for entity_type in self.entity_types:
            lines.append(
                f"- **{entity_type.type}** ({entity_type.name}): {entity_type.description}"
            )

        return "\n".join(lines)

    def _build_extraction_schema(self) -> Dict[str, Any]:
        """
        构建动态JSON Schema（基于数据库中的实体类型配置）

        Returns:
            JSON Schema字典
        """
        # 动态构建实体类型properties
        entity_properties = {}
        for entity_type in self.entity_types:
            entity_properties[entity_type.type] = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "实体名称"},
                        "description": {"type": "string", "description": "实体描述（可选，如职位、角色、定义等）"}
                    },
                    "required": ["name"]
                },
                "description": entity_type.description or entity_type.name,
            }

        # 事项必需字段
        event_required = ["title", "content", "references", "entities"]

        return {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "description": "提取的事项列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "事项标题"},
                            "summary": {"type": "string", "description": "事项摘要"},
                            "content": {"type": "string", "description": "事项详细内容"},
                            "category": {"type": "string", "description": "事项分类（可选，如：技术/产品/市场/研究/管理等）"},
                            "references": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "该事项引用的片段编号列表（从1开始，如 [1, 2]）",
                            },
                            "entities": {
                                "type": "object",
                                "description": "实体字典",
                                "properties": entity_properties,
                            },
                        },
                        "required": event_required,
                    },
                }
            },
            "required": ["events"],
        }

    async def _parse_extraction_result(
        self, result: Dict[str, Any], sections: List[SourceChunk]
    ) -> List[SourceEvent]:
        """
        解析LLM提取结果为SourceEvent对象

        Args:
            result: LLM返回的JSON结果
            sections: 原始片段列表（用于生成引用）

        Returns:
            SourceEvent对象列表
        """
        events = []

        for event_data in result.get("events", []):
            # 解析 LLM 标注的引用（片段编号，从1开始）
            referenced_indices = event_data.get("references", [])

            # 将片段编号转换为实际的 section_id
            referenced_section_ids = []
            invalid_indices = []

            for idx in referenced_indices:
                if isinstance(idx, int) and 1 <= idx <= len(sections):  # 验证索引有效性
                    section = sections[idx - 1]  # 编号从1开始，索引从0开始
                    referenced_section_ids.append(section.id)
                else:
                    # 记录无效索引
                    invalid_indices.append(idx)

            # 记录警告（如果有无效索引）
            if invalid_indices:
                self.logger.warning(
                    f"事项 '{event_data.get('title', '未知')}' 包含无效的片段引用索引: {invalid_indices}",
                    extra={
                        "event_title": event_data.get("title"),
                        "invalid_indices": invalid_indices,
                        "total_sections": len(sections),
                    },
                )

            # 确定主要引用的 chunk（取第一个被引用的 chunk）
            primary_chunk = None
            if referenced_section_ids:
                # 查找第一个被引用的 section 对应的 chunk
                for section in sections:
                    if section.id == referenced_section_ids[0]:
                        primary_chunk = section
                        break
                if not primary_chunk:
                    primary_chunk = sections[0]  # 如果没找到，默认使用第一个 chunk
            else:
                primary_chunk = sections[0] if sections else None

            # 🆕 根据来源类型设置时间
            from datetime import datetime
            from sag.db import ChatMessage
            from sqlalchemy import select
            
            start_time = None
            end_time = None
            event_references = primary_chunk.references if primary_chunk else None
            
            if primary_chunk:
                if primary_chunk.source_type == "ARTICLE":
                    # 文档类型：使用当前时间
                    current_time = datetime.now()
                    start_time = current_time
                    end_time = current_time
                    
                elif primary_chunk.source_type == "CHAT":
                    # 会话类型：从引用的消息中获取时间范围
                    # 使用 primary_chunk.references（因为事项会继承这个）
                    if event_references and isinstance(event_references, list):
                        async with self.session_factory() as session:
                            result_msgs = await session.execute(
                                select(ChatMessage)
                                .where(ChatMessage.id.in_(event_references))
                                .order_by(ChatMessage.timestamp)
                            )
                            messages = list(result_msgs.scalars().all())
                            
                            if messages:
                                start_time = messages[0].timestamp
                                end_time = messages[-1].timestamp
            
            # 创建事项对象
            # 注意：sections 列表已在方法开始时验证为非空
            source_type_value = primary_chunk.source_type if primary_chunk else "ARTICLE"
            event = SourceEvent(
                id=str(uuid.uuid4()),
                source_config_id=self.config.source_config_id,
                source_type=source_type_value,  # 🆕
                source_id=primary_chunk.source_id if primary_chunk else sections[0].source_id,  # 🆕
                article_id=sections[0].article_id if primary_chunk and primary_chunk.source_type == "ARTICLE" else None,  # 🆕 修改
                conversation_id=primary_chunk.conversation_id if primary_chunk and primary_chunk.source_type == "CHAT" else None,  # 🆕
                title=event_data["title"],
                summary=event_data.get("summary") or "",
                content=event_data["content"],
                category=event_data.get("category") or "",  # 独立字段，确保None转为空字符串
                # 业务字段（兼容主系统）- type与source_type保持一致
                type=source_type_value,
                priority="UNKNOWN",  # 默认值
                status="UNKNOWN",  # 默认值
                rank=None,  # 由上层 EventExtractor 统一分配全局 rank，确保同一文章内事项按顺序排列
                start_time=start_time,  # 🆕
                end_time=end_time,  # 🆕
                # 使用 references 字段存储 AI 标注的引用片段（精确引用）
                references=referenced_section_ids,  # ✅ 修复：使用LLM精确标注的引用
                chunk_id=primary_chunk.id if primary_chunk else None,
                extra_data={
                    "quality_score": event_data.get("quality_score", 0.8),
                    "batch_size": len(sections),
                    # category不再存储在extra_data中
                },
            )

            # 解析实体
            entities_data = event_data.get("entities", {})
            event_associations = []

            # 处理每种类型的实体
            for entity_type, entity_names in entities_data.items():
                if not entity_names:
                    continue

                # 查找对应的实体类型定义
                entity_type_obj = self._get_entity_type_by_type(entity_type)
                if not entity_type_obj:
                    self.logger.warning(
                        f"未找到实体类型 '{entity_type}'，跳过该类型的实体提取",
                        extra={"entity_type": entity_type,
                               "event_title": event_data.get("title")},
                    )
                    continue

                for entity_data in entity_names:
                    # 兼容新旧格式：字符串或对象
                    if isinstance(entity_data, dict):
                        name = entity_data.get("name")
                        description = entity_data.get("description", "")
                    else:
                        # 旧格式：直接是字符串
                        name = entity_data
                        description = ""

                    if not name:
                        continue

                    # 获取或创建实体ID（不再传递description）
                    entity_id = await self._get_or_create_entity(
                        entity_type, name, entity_type_obj
                    )

                    # 创建关联对象（description保存到中间表）
                    assoc = EventEntity(
                        id=str(uuid.uuid4()),
                        event_id=event.id,
                        entity_id=entity_id,
                        weight=float(entity_type_obj.weight),
                        description=description or None,  # 保存到中间表
                        extra_data={"confidence": event_data.get(
                            "quality_score", 0.8)},
                    )

                    # 绑定关系
                    event_associations.append(assoc)

            event.event_associations = event_associations
            events.append(event)

        return events

    async def _get_or_create_entity(
        self, entity_type: str, entity_name: str, entity_type_obj: DBEntityType
    ) -> str:
        """
        获取或创建实体的ID（使用新 session）

        先查询数据库是否存在相同 (source_config_id, type, normalized_name) 的实体，
        如果存在则返回其ID，否则创建新实体并返回新ID。

        Args:
            entity_type: 实体类型标识符
            entity_name: 实体原始名称
            entity_type_obj: 实体类型对象

        Returns:
            实体ID
        """
        normalized_name = self._normalize_entity_name(entity_name)

        async with self.session_factory() as session:
            return await self._get_or_create_entity_with_session(
                session, entity_type, entity_name, normalized_name, entity_type_obj
            )

    async def _get_or_create_entity_with_session(
        self,
        session,
        entity_type: str,
        entity_name: str,
        normalized_name: str,
        entity_type_obj: DBEntityType,
    ) -> str:
        """
        获取或创建实体的ID（使用已有 session）

        先查询数据库是否存在相同 (source_config_id, type, normalized_name) 的实体，
        如果存在则返回其ID，否则创建新实体并返回新ID。

        Args:
            session: 数据库 session
            entity_type: 实体类型标识符
            entity_name: 实体原始名称
            normalized_name: 标准化的实体名称
            entity_type_obj: 实体类型对象

        Returns:
            实体ID
        """
        # 查询已存在的实体
        result = await session.execute(
            select(Entity)
            .where(Entity.source_config_id == self.config.source_config_id)
            .where(Entity.type == entity_type)
            .where(Entity.normalized_name == normalized_name)
        )
        existing_entity = result.scalar_one_or_none()

        if existing_entity:
            self.logger.debug(
                f"实体已存在：{entity_name} -> {existing_entity.name} (ID: {existing_entity.id})"
            )
            return existing_entity.id

        # 创建新实体（不保存description）
        new_entity = Entity(
            id=str(uuid.uuid4()),
            source_config_id=self.config.source_config_id,
            entity_type_id=entity_type_obj.id,
            type=entity_type,
            name=entity_name,
            normalized_name=normalized_name,
            description=None,  # 不再保存description到Entity表
            extra_data={},
        )

        # 🆕 解析类型化值
        try:
            value_constraints = entity_type_obj.value_constraints if hasattr(
                entity_type_obj, 'value_constraints') else None
            entity_type_category = entity_type_obj.type if hasattr(
                entity_type_obj, 'type') else None
            typed_fields = self.parser.parse_to_typed_fields(
                entity_name,
                entity_type=entity_type,
                entity_type_category=entity_type_category,  # 🆕 传递属性类型（time/person/location等）
                value_constraints=value_constraints
            )

            # 填充类型化字段
            if typed_fields:
                new_entity.value_type = typed_fields.get("value_type")
                new_entity.value_raw = typed_fields.get("value_raw")
                new_entity.int_value = typed_fields.get("int_value")
                new_entity.float_value = typed_fields.get("float_value")
                new_entity.datetime_value = typed_fields.get("datetime_value")
                new_entity.bool_value = typed_fields.get("bool_value")
                new_entity.enum_value = typed_fields.get("enum_value")
                new_entity.value_unit = typed_fields.get("value_unit")
                new_entity.value_confidence = typed_fields.get(
                    "value_confidence")

                self.logger.debug(
                    f"✅ 解析实体值: {entity_name} -> {typed_fields.get('value_type')} = {typed_fields.get('int_value') or typed_fields.get('float_value') or typed_fields.get('datetime_value') or typed_fields.get('bool_value') or typed_fields.get('enum_value')}"
                )
        except Exception as e:
            # 解析失败不影响实体创建
            self.logger.warning(f"⚠️ 实体值解析失败: {entity_name}, error={e}")

        # 添加到 session（但不立即提交）
        session.add(new_entity)
        await session.flush()  # flush 以获取 ID，但不提交事务

        self.logger.debug(f"创建新实体：{entity_name} (ID: {new_entity.id})")
        return new_entity.id

    def _normalize_entity_name(self, name: str) -> str:
        """
        标准化实体名称

        Args:
            name: 原始名称（可能是字符串或其他类型，如整数）

        Returns:
            标准化后的名称
        """
        import re

        # 先转为字符串，确保能处理非字符串输入（如 LLM 提取的数字实体）
        name_str = str(name)

        # 去除首尾空格并转小写
        normalized = name_str.strip().lower()

        # 去除多余的空格（多个空格合并为一个）
        normalized = re.sub(r"\s+", " ", normalized)

        # 去除常见的标点符号（保留中文标点）
        normalized = re.sub(r"[^\w\s\u4e00-\u9fff]", "", normalized)

        return normalized.strip()

    def _get_entity_type_by_type(self, entity_type: str) -> Optional[DBEntityType]:
        """
        根据类型标识符查找实体类型

        Args:
            entity_type: 实体类型标识符

        Returns:
            实体类型对象，如果未找到返回 None
        """
        for et in self.entity_types:
            if et.type == entity_type:
                return et
        return None

    def _get_entity_type_weight(self, entity_type: str) -> float:
        """
        获取实体类型权重

        Args:
            entity_type: 实体类型

        Returns:
            权重值
        """
        # 从加载的实体类型中查找
        entity_type_obj = self._get_entity_type_by_type(entity_type)
        if entity_type_obj:
            return float(entity_type_obj.weight)

        # 默认权重
        return 1.0
