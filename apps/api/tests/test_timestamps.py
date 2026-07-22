"""API 时间戳必须携带时区，避免浏览器把 UTC 误当成本地时间。"""

from datetime import UTC, timedelta

import pytest


@pytest.mark.asyncio
async def test_sqlite_document_timestamp_serializes_as_utc():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Source
    from sag_api.schemas.document import DocumentOut

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="timestamp-source", sag_source_config_id="timestamp-source-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="timestamp.md",
            content_type="text/markdown",
            size_bytes=1,
            storage_path="/tmp/timestamp.md",
        )
        session.add(document)
        await session.commit()
        await session.refresh(document)

        assert document.created_at.tzinfo is not None
        assert document.created_at.utcoffset() == timedelta(0)
        created_at = DocumentOut.model_validate(document).model_dump(mode="json")["created_at"]
        assert created_at.endswith(("Z", "+00:00"))
        assert document.created_at.tzinfo == UTC
