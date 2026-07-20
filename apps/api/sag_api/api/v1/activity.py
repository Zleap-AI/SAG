"""近期动态 —— 知识库时间线：最近文档（会话不属于知识库动态）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.db import get_session
from sag_api.core.deps import get_current_user
from sag_api.db.models import Document, Source, User

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("")
async def list_activity(
    limit: int = Query(default=20, ge=1, le=50),
    source_ids: list[str] | None = Query(default=None, max_length=100),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    normalized_source_ids = list(
        dict.fromkeys(source_id.strip() for source_id in source_ids or [] if source_id.strip())
    )
    statement = select(Document, Source.name).join(Source, Source.id == Document.source_id)
    if source_ids is not None:
        statement = statement.where(
            Document.source_id.in_(normalized_source_ids)
            if normalized_source_ids
            else false()
        )
    docs = (
        await session.execute(
            statement.order_by(Document.created_at.desc()).limit(limit)
        )
    ).all()
    items: list[dict] = [
        {
            "type": "document",
            "id": d.id,
            "source_id": d.source_id,
            "title": d.filename,
            "subtitle": source_name,
            "status": d.status.value,
            "at": d.created_at.isoformat(),
        }
        for d, source_name in docs
    ]
    items.sort(key=lambda x: x["at"], reverse=True)
    return items[:limit]
