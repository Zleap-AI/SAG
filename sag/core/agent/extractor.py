"""
ExtractorAgent - 事项提取Agent

将文章片段或对话消息转化为结构化事项
"""

from typing import Dict, List
from sag.core.agent.base import BaseAgent
from sag.core.prompt import get_prompt_manager
from sag.db import SourceChunk
from sag.utils import get_logger

logger = get_logger("agent.extractor")


class ExtractorAgent(BaseAgent):
    """
    提取Agent - 从chunk提取结构化事项
    
    核心流程：
    1. memory: 加载文章/会话元数据（背景）
    2. database: 加载实体类型定义 + 待处理内容
    3. todo: 设置7步提取任务
    4. 执行: 返回JSON格式的events
    """
    
    def __init__(self, chunk_type: str, **kwargs):
        """
        Args:
            chunk_type: 'article' 或 'conversation'
            **kwargs: 传递给BaseAgent（如model_config）
        """
        # 加载extractor.json
        prompt_manager = get_prompt_manager()
        agent_config = prompt_manager.load_json_config("extractor")
        
        # 从配置中读取各部分
        config_data = agent_config.get('config', {})
        
        kwargs.update({
            'database': config_data.get('database', []),
            'memory': config_data.get('memory', []),
            'todo': config_data.get('todo', []),
            'output': config_data.get('output', {}),
            'scenario': 'extract'
        })
        
        super().__init__(**kwargs)
        
        self.chunk_type = chunk_type
        logger.info(f"ExtractorAgent初始化: type={chunk_type}")
    
    async def extract(
        self,
        content_items: List,
        metadata: Dict,
        entity_types: List[Dict],
        chunk: SourceChunk
    ) -> Dict:
        """
        执行提取
        
        Args:
            content_items: 待处理内容（sections或messages）
            metadata: 文章/会话元数据
            entity_types: 实体类型定义（必传，非空）
            chunk: chunk对象
        
        Returns:
            {"events": [...]}
        """
        logger.info(
            f"提取: chunk={chunk.id}, type={self.chunk_type}, "
            f"items={len(content_items)}, entity_types={len(entity_types)}"
        )
        
        # 1. memory: 背景信息
        self._load_background(metadata)
        
        # 2. database: 实体类型 + 待处理内容
        self._load_entity_types(entity_types)
        self._load_content_items(content_items)
        
        # 3. todo: 7步任务
        self._setup_tasks(len(content_items), len(entity_types))
        
        # 4. 构建查询
        query = self._build_query(len(content_items), metadata.get('title', ''))
        
        # 5. 动态添加实体类型约束到 schema
        from copy import deepcopy
        schema = deepcopy(self.output_config.get('schema'))
        
        if schema and entity_types:
            # 提取所有有效的实体类型
            valid_entity_types = [et['type'] for et in entity_types]
            
            # 导航到 entities.type 字段并添加 enum 约束
            try:
                entity_type_schema = (
                    schema
                    .get('properties', {})
                    .get('events', {})
                    .get('items', {})
                    .get('properties', {})
                    .get('entities', {})
                    .get('items', {})
                    .get('properties', {})
                    .get('type', {})
                )
                
                if entity_type_schema is not None:
                    # ✅ 添加 enum 约束，强制 LLM 只能使用预定义的类型
                    entity_type_schema['enum'] = valid_entity_types
                    logger.info(f"已添加实体类型约束: {valid_entity_types}")
            except (KeyError, AttributeError, TypeError) as e:
                logger.warning(f"无法添加实体类型约束: {e}")
        
        # 6. 执行（使用修改后的 schema）
        result = await self.run(query=query, schema=schema)
        
        # schema 模式下，result 已经是解析好的 JSON 对象
        logger.info(f"提取完成: events={len(result.get('events', []))}")
        return result
    
    def _load_background(self, metadata: Dict):
        """
        Memory：背景知识（辅助理解，不干扰）
        - 分区1：源背景（Article/Conversation表字段）
        - 分区2：上文chunk内容（如果有，提供承接关系）
        """
        chunk_rank = metadata.get("chunk_rank", 0)
        chunk_heading = metadata.get("chunk_heading", "")
        
        if self.chunk_type == 'ARTICLE':
            # Memory 分区1：文章背景
            self.add_memory(
                data_type="文章背景",
                items=[{
                    "标题": metadata.get("title"),
                    "摘要": metadata.get("summary"),
                    "分类": metadata.get("category"),
                    "标签": metadata.get("tags")
                }],
                description=f"📄 Article表字段 - 当前处理：第{chunk_rank + 1}段《{chunk_heading}》"
            )
            
            # Memory 分区2：上文片段（如果有）
            previous = metadata.get("previous_chunk")
            if previous:
                self.add_memory(
                    data_type="上文片段",
                    items=[{
                        "标题": previous.get("heading"),
                        "内容": previous.get("content")
                    }],
                    description="📎 前一片段的完整内容 - 提供上下文，帮助理解承接关系"
                )
        
        else:  # CHAT
            # Memory 分区1：会话背景
            self.add_memory(
                data_type="会话背景",
                items=[{
                    "对话主题": metadata.get("title"),
                    "平台": metadata.get("platform"),
                    "场景": metadata.get("scenario"),
                    "参与者": metadata.get("participants"),
                    "消息总数": metadata.get("messages_count"),
                    "当前时间段": metadata.get("time_range")
                }],
                description=f"💬 Conversation表字段 - 当前处理：第{chunk_rank + 1}段《{chunk_heading}》"
            )
            
            # Memory 分区2：上文对话（如果有）
            previous = metadata.get("previous_chunk")
            if previous:
                self.add_memory(
                    data_type="上文对话",
                    items=[{
                        "时间段": previous.get("heading"),
                        "对话内容": previous.get("content")
                    }],
                    description="📎 前一时间段的完整对话 - 提供上下文，帮助理解对话进展"
                )
    
    def _load_entity_types(self, entity_types: List[Dict]):
        """
        database第1分区: 实体类型定义（必传）
        
        Args:
            entity_types: [{"type": "person", "name": "人物", "description": "...", ...}]
        """
        self.add_database(
            data_type="实体类型定义",
            items=[{
                "type": et['type'],
                "name": et['name'],
                "description": et.get('description', ''),
                "weight": et.get('weight', 1.0),
                "examples": et.get('examples', [])
            } for et in entity_types],
            description=f"📋 实体类型清单（{len(entity_types)}种）- 提取entities时type必须从这里选择"
        )
    
    def _load_content_items(self, items: List):
        """database第2分区: 待处理内容"""
        if self.chunk_type == 'ARTICLE':
            self.add_database(
                data_type="待处理文章片段",
                items=[{
                    "section_id": s.id,
                    "rank": s.rank,
                    "heading": s.heading,
                    "content": s.content
                } for s in items],
                description=f"📄 待处理的{len(items)}个文章片段 - 提取事项并在references中引用section_id"
            )
        else:  # CHAT
            self.add_database(
                data_type="待处理对话消息",
                items=[{
                    "message_id": m.id,
                    "timestamp": m.timestamp.isoformat() if m.timestamp else "",
                    "sender_name": m.sender_name or "未知",
                    "sender_role": m.sender_role or "USER",
                    "content": m.content or ""
                } for m in items],
                description=f"💬 待处理的{len(items)}条对话消息 - 提取事项并在references中引用message_id"
            )
    
    def _setup_tasks(self, items_count: int, types_count: int):
        """todo：7步提取流程"""
        self.clear_todo()
        
        content_type = "文章片段" if self.chunk_type == 'ARTICLE' else "对话消息"
        id_field = "section_id" if self.chunk_type == 'ARTICLE' else "message_id"
        
        tasks = [
            ("understand", f"理解背景：阅读memory的源信息和上文内容，把握主题和承接关系", 10),
            ("learn-types", f"学习规则：记住{types_count}种实体类型", 9),
            ("scan", f"扫描数据：浏览{items_count}个{content_type}，规划拆分", 8),
            ("extract", f"提取事项：拆分单元，自然规整（title概括、summary说明、content完整描述）", 7),
            ("entities", f"识别实体：提取关键实体，使用定义的type", 6),
            ("references", f"精确引用：添加{id_field}，覆盖所有ID", 5),
            ("verify", f"验证质量：检查完整性和准确性", 4)
        ]
        
        for task_id, desc, priority in tasks:
            self.add_todo(task_id=task_id, description=desc, status="pending", priority=priority)
    
    def _build_query(self, items_count: int, title: str) -> str:
        """构建提取查询（简洁明了）"""
        content_type = "文章片段" if self.chunk_type == 'ARTICLE' else "对话消息"
        
        return (
            f"从database提取{items_count}个{content_type}的结构化事项。\n"
            f"主题：{title}\n\n"
            f"参考memory理解背景，严格按todo步骤执行。"
        )

