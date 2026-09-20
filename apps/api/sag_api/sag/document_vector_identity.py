"""Persist document embedding identities and derive the source-wide state."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.db.models import Document, Source
from sag_api.enums import DocumentStatus
from sag_api.sag.octx_vector_protocol import reconcile_vector_identity_records


async def refresh_source_vector_identity(
    session: AsyncSession,
    source: Source,
) -> bool:
    """Recompute the export identity from all active, exportable documents."""
    await session.refresh(source, attribute_names=["config"])
    identities = list(
        (
            await session.scalars(
                select(Document.vector_identity).where(
                    Document.source_id == source.id,
                    Document.is_active.is_(True),
                    Document.status == DocumentStatus.READY,
                )
            )
        ).all()
    )
    return reconcile_vector_identity_records(source, identities)


async def record_document_vector_identity(
    session: AsyncSession,
    source: Source,
    document: Document,
    identity: dict[str, Any] | None,
) -> bool:
    """Record a successful vector write, then refresh the source aggregate."""
    document.vector_identity = dict(identity) if isinstance(identity, dict) else None
    await session.flush()
    return await refresh_source_vector_identity(session, source)
