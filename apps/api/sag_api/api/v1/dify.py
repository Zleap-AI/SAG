"""Dify 外部知识库兼容层。"""

from __future__ import annotations

from secrets import compare_digest

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.db import get_session
from sag_api.core.deps import get_engine_manager
from sag_api.core.errors import (
    ForbiddenError,
    ServiceUnavailableError,
    ValidationError,
)
from sag_api.sag import EngineManager
from sag_api.schemas.dify import (
    DifyRetrievalRecord,
    DifyRetrievalRequest,
    DifyRetrievalResponse,
)
from sag_api.services.retrieval_service import retrieve_relevant_sections
from sag_api.services.source_service import get_source

router = APIRouter(prefix="/dify", tags=["dify"])


def _require_dify_api_key(
    authorization: str | None = Header(default=None),
) -> None:
    expected = settings.dify_api_key
    if not expected:
        raise ServiceUnavailableError("Dify integration is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not supplied
        or not compare_digest(supplied, expected)
    ):
        raise ForbiddenError("Invalid Dify API key")


def _section_title(source_name: str, heading: str) -> str:
    heading = heading.strip()
    return f"{source_name} — {heading}" if heading else source_name


@router.post("/retrieval", response_model=DifyRetrievalResponse)
async def retrieval(
    body: DifyRetrievalRequest,
    _authorized: None = Depends(_require_dify_api_key),
    session: AsyncSession = Depends(get_session),
    engine_manager: EngineManager = Depends(get_engine_manager),
) -> DifyRetrievalResponse:
    """Adapt one SAG source to Dify's external knowledge retrieval contract."""

    knowledge_id = body.knowledge_id.strip()
    query = body.query.strip()
    if not knowledge_id and not query:
        return DifyRetrievalResponse()
    if not knowledge_id or not query:
        raise ValidationError(
            "knowledge_id and query are required for retrieval"
        )
    if body.metadata_condition is not None:
        raise ValidationError(
            "metadata_condition is not supported by the SAG Dify integration"
        )

    source = await get_source(session, knowledge_id)
    strategy = settings.dify_search_strategy
    outcome = await retrieve_relevant_sections(
        engine_manager,
        [source],
        query,
        strategy=strategy,
        top_k=body.retrieval_setting.top_k,
    )
    effective_strategy = str(
        outcome.stats.get("effective_strategy")
        or outcome.stats.get("strategy")
        or strategy
    )
    fallback_used = bool(outcome.stats.get("fallback_used", False))
    records = [
        DifyRetrievalRecord(
            content=section.content,
            title=_section_title(source.name, section.heading),
            score=section.score,
            metadata={
                "document_id": (
                    f"{source.id}:{section.chunk_id}"
                    if section.chunk_id
                    else source.id
                ),
                "source_id": source.id,
                "source_name": source.name,
                "chunk_id": section.chunk_id or "",
                "heading": section.heading,
                "retrieval_strategy": effective_strategy,
                "fallback_used": fallback_used,
            },
        )
        for section in outcome.sections
        if section.score >= body.retrieval_setting.score_threshold
    ]
    return DifyRetrievalResponse(records=records)
