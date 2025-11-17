"""
事项内容扩展服务

负责为搜索结果中的事项列表补充完整信息：
- 关联实体
- 原文片段引用
"""

from typing import List, Dict, Any, Union
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sag.db.models import SourceEvent, EventEntity, ArticleSection
from sag.api.schemas.document import SourceEventResponse


class EventEnricher:
    """事项内容扩展器"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def enrich_events(self, events: List[Any]) -> List[Dict[str, Any]]:
        """
        为事项列表补充完整信息

        Args:
            events: 事项列表（可以是字典或 Pydantic 模型）

        Returns:
            扩展后的事项字典列表（包含 entities 和 references）
        """
        if not events:
            return []

        # 提取事项 ID
        event_ids = []
        for event in events:
            if isinstance(event, dict):
                event_ids.append(event.get('id'))
            else:
                event_ids.append(getattr(event, 'id', None))

        event_ids = [eid for eid in event_ids if eid]  # 过滤 None

        if not event_ids:
            return []

        # 1. 批量查询事项及其关联的实体（同时预加载 source 和 article）
        query = (
            select(SourceEvent)
            .where(SourceEvent.id.in_(event_ids))
            .options(
                selectinload(SourceEvent.event_associations).selectinload(EventEntity.entity),
                selectinload(SourceEvent.source),  # 预加载 SourceConfig
                selectinload(SourceEvent.article)  # 预加载 Article
            )
        )
        result = await self.db.execute(query)
        db_events_list = result.scalars().all()

        # 为每个事项添加 source_name 和 document_name 属性
        for event in db_events_list:
            event.source_name = event.source.name if event.source else ""
            event.document_name = event.article.title if event.article else ""

        db_events = {event.id: event for event in db_events_list}

        # 2. 从数据库查询的 ORM 对象收集所有需要查询的 section_ids
        all_section_ids = set()
        for event_id, db_event in db_events.items():
            # 从 ORM 对象读取 references 字段（这是一个 ID 数组）
            if db_event.references and isinstance(db_event.references, list):
                all_section_ids.update(db_event.references)
                print(f"📎 事项 {event_id} 的 references: {db_event.references[:2] if len(db_event.references) > 2 else db_event.references}")

        print(f"📊 总共收集到 {len(all_section_ids)} 个唯一的片段ID需要查询")

        # 3. 批量查询所有片段
        sections_dict = {}
        if all_section_ids:
            sections_query = select(ArticleSection).where(
                ArticleSection.id.in_(list(all_section_ids))
            )
            sections_result = await self.db.execute(sections_query)
            sections_dict = {section.id: section for section in sections_result.scalars().all()}
            print(f"✅ 成功查询到 {len(sections_dict)} 个片段")
        else:
            print("⚠️ 没有需要查询的片段ID")

        # 4. 为每个事项使用标准转换方法
        enriched_events = []
        for event in events:
            # 获取事项 ID
            if isinstance(event, dict):
                event_id = event.get('id')
            else:
                event_id = getattr(event, 'id', None)

            if not event_id or event_id not in db_events:
                continue

            # 使用标准转换方法（与文档管理一致）
            db_event = db_events[event_id]
            event_response = SourceEventResponse.from_orm_with_entities(
                db_event,
                sections_dict
            )

            # 转换为字典
            enriched_events.append(event_response.model_dump())

        print(f"✅ 成功处理 {len(enriched_events)} 个事项（已扩展实体和引用）")
        return enriched_events
