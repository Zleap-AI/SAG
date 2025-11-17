"""
线索追踪器（Tracker）

统一的线索和节点构建工具，融合了原Cluer和ClueTracker的功能。

核心功能：
1. 节点构建：build_xxx_node() 方法，生成标准化的节点格式
2. 线索添加：add_clue() 方法，直接追加到 config.all_clues
3. ID管理：自动生成和验证节点ID
4. 格式规范：所有节点包含 {id, type, category, content, description}

使用方式：
    # 创建追踪器实例
    tracker = Tracker(config)

    # 添加线索
    tracker.add_clue(
        stage="recall",
        from_node=Tracker.build_query_node(config),
        to_node=Tracker.build_entity_node(entity),
        confidence=0.85,
        relation="语义相似",
        metadata={"method": "vector_search", "step": "step1"}
    )
"""

import uuid
from typing import Any, Dict, Optional
import logging

from sag.db import SourceEvent
from sag.modules.search.config import SearchConfig

# 获取logger
logger = logging.getLogger(__name__)


class Tracker:
    """
    线索追踪器 - 统一的节点和线索管理

    融合原Cluer和ClueTracker的功能：
    - 静态方法：用于构建标准化的节点
    - 实例方法：用于管理线索的生命周期
    """

    def __init__(self, config: SearchConfig):
        """
        初始化线索追踪器

        Args:
            config: 搜索配置，线索会追加到 config.all_clues
        """
        self.config = config
        # 阶段内 event ID 映射：{stage: {event_db_id: node_id}}
        # 用于实现：同一阶段内重复召回同一 event 时，复用相同的节点 ID
        self._stage_event_map: Dict[str, Dict[str, str]] = {}

    # ========== ID生成方法 ==========

    @staticmethod
    def generate_query_id(query: str) -> str:
        """
        生成query节点的确定性ID

        使用UUID5确保同一查询生成相同ID，便于前端图谱合并节点

        Args:
            query: 查询字符串

        Returns:
            确定性UUID字符串
        """
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, query))

    @staticmethod
    def generate_clue_id() -> str:
        """
        生成线索的唯一ID

        使用UUID4确保每条线索都有唯一ID

        Returns:
            随机UUID字符串
        """
        return str(uuid.uuid4())

    # ========== 节点构建方法 ==========

    @staticmethod
    def build_query_node(
        config: SearchConfig,
        use_origin: bool = False
    ) -> Dict[str, Any]:
        """
        构建query节点

        Args:
            config: 搜索配置
            use_origin: 是否使用原始查询（True）还是当前查询（False）

        Returns:
            标准格式的query节点: {id, type, category, content, description}
        """
        # 确定使用哪个查询
        query_text = config.original_query if use_origin else config.query

        # 确定category和description
        if config.original_query and config.original_query != config.query:
            # 有重写
            category = "origin" if use_origin else "rewrite"
            description = "原始搜索内容" if use_origin else "重写的请求"
        else:
            # 无重写
            category = "origin"
            description = "原始搜索内容"

        return {
            "id": Tracker.generate_query_id(query_text),
            "type": "query",
            "category": category,
            "content": query_text,
            "description": description
        }

    @staticmethod
    def build_entity_node(entity: Dict[str, Any]) -> Dict[str, Any]:
        """
        构建entity节点

        Args:
            entity: 实体字典，应包含 key_id/id/entity_id, name, type, description

        Returns:
            标准格式的entity节点: {id, type, category, content, description}
        """
        # 兼容不同的ID字段名
        entity_id = entity.get("key_id") or entity.get("id") or entity.get("entity_id")

        # 验证：确保entity_id存在
        if not entity_id:
            logger.warning(
                f"⚠️ [Tracker] 实体缺少ID字段！entity={entity}，将使用fallback ID"
            )
            # Fallback: 使用name生成确定性ID
            entity_name = entity.get("name", "unknown")
            entity_id = f"fallback-{uuid.uuid5(uuid.NAMESPACE_DNS, entity_name)}"

        return {
            "id": entity_id,
            "type": "entity",
            "category": entity.get("type") or "unknown",  # person/topic/location等
            "content": entity.get("name") or "",
            "description": entity.get("description") or "",  # 确保None转为空字符串
            "hop": entity.get("hop", 0)  # 🎨 添加hop字段（用于前端颜色渐变）
        }
    
    @staticmethod
    def build_extracted_entity_node(attribute: Dict[str, Any]) -> Dict[str, Any]:
        """
        构建从query提取的属性节点（用于prepare阶段）
        
        专门用于 LLM 从查询中提取的实体属性，与数据库中的实体区分
        
        Args:
            attribute: LLM提取的属性 {name, type, description, confidence}
            
        Returns:
            标准格式的entity节点
        """
        entity_name = attribute.get("name", "")
        entity_type = attribute.get("type", "unknown")
        
        # 生成确定性ID（使用 extracted- 前缀区分）
        entity_id = f"extracted-{uuid.uuid5(uuid.NAMESPACE_DNS, f'{entity_type}:{entity_name}')}"
        
        return {
            "id": entity_id,
            "type": "entity",
            "category": entity_type,
            "content": entity_name,
            "description": attribute.get("description", "") or "从查询提取的属性"
        }

    @staticmethod
    def build_event_node(event: SourceEvent, stage: Optional[str] = None, hop: Optional[int] = None) -> Dict[str, Any]:
        """
        构建event节点

        Args:
            event: 事项对象
            stage: 阶段标识（用于生成阶段隔离的节点ID和显示标签）
            hop: 跳数（仅 expand 阶段使用，用于显示标签）

        Returns:
            标准格式的event节点: {id, event_id, type, category, content, description, stage, hop}
            - id: 阶段隔离的节点ID（如果提供stage，格式为 {stage}_{event.id}）
            - event_id: 数据库原始ID（用于前端查询详情）
            - content: 使用 title（简短标题，适合节点显示）
            - description: 使用 content（完整内容，适合详情页）
            - stage: 阶段标识（用于前端显示标签）
            - hop: 跳数（用于前端显示标签）
        """
        # 如果提供了 stage，生成阶段隔离的 ID
        node_id = f"{stage}_{event.id}" if stage else event.id

        node = {
            "id": node_id,
            "event_id": event.id,                # 数据库原始 ID
            "type": "event",
            "category": event.category or "",    # 直接使用 category 字段
            "content": event.title or "",        # 使用标题作为节点显示内容
            "description": event.content or ""   # 使用完整内容作为描述
        }

        # 如果提供了 stage，添加到节点中
        if stage:
            node["stage"] = stage

        # 如果提供了 hop，添加到节点中
        if hop is not None:
            node["hop"] = hop

        return node

    @staticmethod
    def build_section_node(section: Dict[str, Any]) -> Dict[str, Any]:
        """
        构建section节点

        Args:
            section: 段落字典，应包含 id/section_id, section_type, content, summary

        Returns:
            标准格式的section节点: {id, type, category, content, description}
        """
        # 兼容不同的ID字段
        section_id = section.get("section_id") or section.get("id")

        # 验证：确保section_id存在
        if not section_id:
            logger.warning(
                f"⚠️ [Tracker] 段落缺少ID字段！section={section}，将使用fallback ID"
            )
            section_id = f"fallback-section-{uuid.uuid4()}"

        return {
            "id": section_id,
            "type": "section",
            "category": section.get("section_type", ""),
            "content": section.get("heading", section.get("content", ""))[:50],  # 截取前50字符
            "description": section.get("summary", "")
        }

    # ========== 线索构建方法（静态，用于直接构建） ==========

    @staticmethod
    def build_clue(
        stage: str,
        from_node: Dict[str, Any],
        to_node: Dict[str, Any],
        confidence: float,
        relation: str,
        metadata: Optional[Dict[str, Any]] = None,
        display_level: str = "intermediate"
    ) -> Dict[str, Any]:
        """
        构建完整的线索对象

        Args:
            stage: 阶段标识 (recall/expand/rerank)
            from_node: 起点节点（标准格式）
            to_node: 终点节点（标准格式）
            confidence: 置信度分数
            relation: 关系类型
            metadata: 元数据字典（可包含 step, hop, method 等）
            display_level: 显示级别，用于前端图谱精简控制
                - "final": 最终结果，精简模式显示
                - "intermediate": 中间步骤，仅全量模式显示
                - "debug": 调试信息，仅调试模式显示

        Returns:
            完整的线索字典
        """
        # 验证：确保confidence在[0, 10]范围内
        if confidence < 0.0 or confidence > 10.0:
            logger.warning(
                f"⚠️ [Tracker] 置信度超出范围 [0,10]: confidence={confidence:.4f}，"
                f"stage={stage}，from={from_node.get('id', 'N/A')[:8]}，"
                f"to={to_node.get('id', 'N/A')[:8]}"
            )
            # 限制在 [0, 10] 范围内
            confidence = max(0.0, min(10.0, confidence))

        # 验证：记录零置信度（调试用）
        if confidence == 0.0:
            logger.debug(
                f"🔍 [Tracker] 零置信度线索: stage={stage}, "
                f"from={from_node.get('content', 'N/A')}, "
                f"to={to_node.get('content', 'N/A')}"
            )

        return {
            "id": Tracker.generate_clue_id(),
            "stage": stage,
            "from": from_node,
            "to": to_node,
            "confidence": confidence,
            "relation": relation,
            "metadata": metadata or {},
            "display_level": display_level  # 🆕 显示级别控制
        }

    # ========== 实例方法：线索管理（融合ClueTracker功能） ==========

    def get_or_create_event_node(
        self,
        event: SourceEvent,
        stage: str,
        hop: Optional[int] = None,
        recall_method: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        获取或创建阶段内唯一的 event 节点

        实现逻辑：
        - Recall 阶段：同一 event 只生成一个节点（复用节点 ID）
        - Expand 阶段：同一 event 在不同跳生成不同节点（按 hop 区分）
          例如：第1跳召回 event_A，第2跳再次召回 event_A，会生成2个不同的节点
          这样可以在图谱中看到同一事项在多跳搜索中的传播路径
        - Rerank 阶段：同一 event 通过不同召回方式生成不同节点（按 recall_method 区分）
          例如：entity召回的 event_A 和 section召回的 event_A 是两个不同的节点
          这样可以在图谱中区分不同的召回路径

        Args:
            event: 事项对象
            stage: 阶段标识 (recall/expand/rerank)
            hop: 跳数（expand 阶段必须提供，用于区分不同跳的节点）
            recall_method: 召回方式（rerank 阶段使用，如 "entity_recall", "section_recall"）

        Returns:
            标准格式的 event 节点
        """
        # 初始化该阶段的映射表
        if stage not in self._stage_event_map:
            self._stage_event_map[stage] = {}

        # 🆕 Expand 阶段：同一 event 在不同跳生成不同节点
        if stage == "expand" and hop is not None:
            # 使用 (event_id, hop) 作为缓存 key，确保不同跳生成不同节点
            cache_key = f"{event.id}_hop{hop}"

            if cache_key in self._stage_event_map[stage]:
                # 同一跳内复用节点
                node_id = self._stage_event_map[stage][cache_key]
                logger.debug(
                    f"🔄 [Tracker] 复用 expand 第{hop}跳事项节点: "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )
            else:
                # 新跳数，生成新节点（使用 UUID）
                node_id = f"expand_hop{hop}_{event.id}_{str(uuid.uuid4())[:8]}"
                self._stage_event_map[stage][cache_key] = node_id
                logger.debug(
                    f"✨ [Tracker] 创建 expand 第{hop}跳新事项节点: "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )
        # 🆕 Rerank 阶段：按召回方式区分节点
        elif stage == "rerank" and recall_method:
            # 使用 (event_id, recall_method) 作为缓存 key
            cache_key = f"{event.id}_{recall_method}"

            if cache_key in self._stage_event_map[stage]:
                # 相同召回方式复用节点
                node_id = self._stage_event_map[stage][cache_key]
                logger.debug(
                    f"🔄 [Tracker] 复用 rerank {recall_method} 事项节点: "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )
            else:
                # 新召回方式，生成新节点
                node_id = f"rerank_{recall_method}_{event.id}_{str(uuid.uuid4())[:8]}"
                self._stage_event_map[stage][cache_key] = node_id
                logger.debug(
                    f"✨ [Tracker] 创建 rerank {recall_method} 新事项节点: "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )
        else:
            # Recall 阶段或其他：同一 event 复用节点 ID
            if event.id in self._stage_event_map[stage]:
                # 复用之前的节点 ID
                node_id = self._stage_event_map[stage][event.id]
                logger.debug(
                    f"🔄 [Tracker] 复用阶段内事项节点: stage={stage}, "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )
            else:
                # 首次召回，生成新的阶段隔离 ID
                node_id = f"{stage}_{event.id}"
                self._stage_event_map[stage][event.id] = node_id
                logger.debug(
                    f"✨ [Tracker] 创建新事项节点: stage={stage}, "
                    f"event_id={event.id[:8]}, node_id={node_id[:8]}"
                )

        # 构建节点（使用统一的 ID）
        node = {
            "id": node_id,
            "event_id": event.id,                # 数据库原始 ID
            "type": "event",
            "category": event.category or "",
            "content": event.title or "",
            "description": event.content or "",
            "stage": stage  # 🆕 添加阶段标识
        }

        # 如果提供了 hop 值，添加到节点中
        if hop is not None:
            node["hop"] = hop

        return node

    def add_clue(
        self,
        stage: str,
        from_node: Dict[str, Any],
        to_node: Dict[str, Any],
        confidence: float = 1.0,
        relation: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        display_level: str = "intermediate"
    ) -> Dict[str, Any]:
        """
        添加线索到 config.all_clues（融合ClueTracker功能）

        这是统一的线索添加接口，会：
        1. 验证节点格式（必须包含 id, type, content）
        2. 自动生成线索ID
        3. 截断置信度到 [0, 1]
        4. 直接追加到 config.all_clues
        5. 返回构建的线索对象

        Args:
            stage: 阶段标识 (recall/expand/rerank)
            from_node: 起点节点（标准格式）
            to_node: 终点节点（标准格式）
            confidence: 置信度分数 [0, 1]
            relation: 关系类型（可选，会自动推断）
            metadata: 元数据字典（建议包含 method, step, hop 等）
            display_level: 显示级别（final/intermediate/debug）

        Returns:
            构建的线索对象

        Example:
            tracker = Tracker(config)
            tracker.add_clue(
                stage="recall",
                from_node=Tracker.build_query_node(config),
                to_node=Tracker.build_entity_node(entity),
                confidence=0.85,
                relation="语义相似",
                metadata={"method": "vector_search", "step": "step1"},
                display_level="intermediate"
            )
        """
        # 验证节点格式
        for node_name, node in [("from_node", from_node), ("to_node", to_node)]:
            if not isinstance(node, dict):
                raise ValueError(f"{node_name} must be a dict, got {type(node)}")
            if "id" not in node or "type" not in node:
                raise ValueError(
                    f"{node_name} must contain 'id' and 'type' fields. Got: {node.keys()}"
                )

        # 检查是否已存在相同的线索
        # 🆕 优化去重规则：from_id + to_id 相同即认为是重复（忽略 display_level）
        # 这样可以避免同一路径生成多条不同 display_level 的线索
        # 注意：不检查 stage，因为同一连接在不同阶段出现应该去重
        from_id = from_node["id"]
        to_id = to_node["id"]

        # 遍历已有线索，判断是否存在相同路径的线索
        existing_clue = next(
            (clue for clue in self.config.all_clues
             if clue["from"]["id"] == from_id
             and clue["to"]["id"] == to_id),
            None
        )

        if existing_clue:
            # 🆕 检查优先级：如果新线索的 display_level 优先级更高，则更新现有线索
            new_priority = self._get_display_level_priority(display_level)
            old_priority = self._get_display_level_priority(existing_clue["display_level"])

            if new_priority > old_priority:
                # 更新为更高优先级的 display_level 和相关信息
                old_display_level = existing_clue["display_level"]
                existing_clue["display_level"] = display_level
                existing_clue["stage"] = stage  # 🆕 同时更新 stage
                existing_clue["confidence"] = confidence
                existing_clue["relation"] = relation if relation else self._get_default_relation(stage)
                if metadata:
                    existing_clue["metadata"] = metadata

                logger.debug(
                    f"🔄 [Tracker] 线索优先级升级: "
                    f"{from_node['type']}→{to_node['type']}, "
                    f"{old_display_level} → {display_level}, "
                    f"stage={stage}"
                )
            else:
                logger.debug(
                    f"🔄 [Tracker] 线索已存在且优先级更高，跳过更新: "
                    f"{from_node['type']}→{to_node['type']}, "
                    f"existing={existing_clue['display_level']} (优先级={old_priority}), "
                    f"new={display_level} (优先级={new_priority})"
                )

            return existing_clue

        # 自动推断relation
        if relation is None:
            relation = self._get_default_relation(stage)

        # 构建线索
        clue = self.build_clue(
            stage=stage,
            from_node=from_node,
            to_node=to_node,
            confidence=confidence,
            relation=relation,
            metadata=metadata,
            display_level=display_level
        )

        # 追加到config.all_clues
        self.config.all_clues.append(clue)

        return clue

    def _get_default_relation(self, stage: str) -> str:
        """
        获取默认的关系类型

        Args:
            stage: 阶段标识

        Returns:
            默认关系类型
        """
        relation_map = {
            "recall": "语义相似",
            "expand": "关系扩展",
            "rerank": "内容重排"
        }
        return relation_map.get(stage, "未知关系")

    @staticmethod
    def _get_display_level_priority(level: str) -> int:
        """
        获取 display_level 的优先级

        优先级规则：
        - debug: 0 (最低优先级，调试信息)
        - intermediate: 1 (中间过程)
        - final: 2 (最高优先级，最终结果)

        当同一路径（from_id → to_id）有多条线索时，
        优先保留优先级最高的线索（final > intermediate > debug）

        Args:
            level: display_level 值

        Returns:
            优先级数值（越大优先级越高）
        """
        priority_map = {
            "debug": 0,
            "intermediate": 1,
            "final": 2,
        }
        return priority_map.get(level, 1)  # 默认返回 intermediate 的优先级

    # ========== 便利方法（保留向后兼容性） ==========

    @staticmethod
    def build_recall_clue(
        config: SearchConfig,
        entity: Dict[str, Any],
        confidence: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        构建Recall阶段线索 (query → entity)

        注意：这是静态方法，仅构建线索对象，不会追加到config.all_clues
        推荐使用实例方法 add_clue() 代替

        Args:
            config: 搜索配置
            entity: 实体字典
            confidence: 置信度
            metadata: 元数据

        Returns:
            Recall线索对象
        """
        query_node = Tracker.build_query_node(config, use_origin=False)
        entity_node = Tracker.build_entity_node(entity)

        return Tracker.build_clue(
            stage="recall",
            from_node=query_node,
            to_node=entity_node,
            confidence=confidence,
            relation="语义相似",
            metadata=metadata
        )

    @staticmethod
    def build_expand_clue(
        parent_entity: Dict[str, Any],
        child_entity: Dict[str, Any],
        confidence: float,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        构建Expand阶段线索 (entity → entity)

        注意：这是静态方法，仅构建线索对象，不会追加到config.all_clues
        推荐使用实例方法 add_clue() 代替

        Args:
            parent_entity: 父实体字典
            child_entity: 子实体字典
            confidence: 置信度
            metadata: 元数据

        Returns:
            Expand线索对象
        """
        from_node = Tracker.build_entity_node(parent_entity)
        to_node = Tracker.build_entity_node(child_entity)

        return Tracker.build_clue(
            stage="expand",
            from_node=from_node,
            to_node=to_node,
            confidence=confidence,
            relation="关系扩展",
            metadata=metadata
        )

    @staticmethod
    def build_rerank_clue(
        entity: Dict[str, Any],
        event: SourceEvent,
        confidence: float,
        relation: str = "内容重排",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        构建Rerank阶段线索 (entity → event)

        注意：这是静态方法，仅构建线索对象，不会追加到config.all_clues
        推荐使用实例方法 add_clue() 代替

        Args:
            entity: 实体字典
            event: 事项对象
            confidence: 置信度
            relation: 关系类型（默认"内容重排"）
            metadata: 元数据

        Returns:
            Rerank线索对象
        """
        from_node = Tracker.build_entity_node(entity)
        to_node = Tracker.build_event_node(event)

        return Tracker.build_clue(
            stage="rerank",
            from_node=from_node,
            to_node=to_node,
            confidence=confidence,
            relation=relation,
            metadata=metadata
        )
