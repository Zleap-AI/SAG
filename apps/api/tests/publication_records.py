"""Real business publication records for retrieval boundary tests."""

import pytest
from sqlalchemy import delete

from sag_api.core import db
from sag_api.db.models import Document
from sag_api.enums import DocumentStatus


@pytest.fixture
async def published_inputs():
    document_ids = []

    async def publish(source_id: str, *input_ids: str):
        async with db.SessionLocal() as session:
            documents = [
                Document(
                    source_id=source_id,
                    filename=f"{input_id}.md",
                    storage_path=f"/test-publications/{input_id}.md",
                    status=DocumentStatus.READY,
                    sag_source_id=input_id,
                    control_revision=1,
                )
                for input_id in input_ids
            ]
            session.add_all(documents)
            await session.commit()
            document_ids.extend(document.id for document in documents)

    yield publish
    if document_ids:
        async with db.SessionLocal() as session:
            await session.execute(delete(Document).where(Document.id.in_(document_ids)))
            await session.commit()


@pytest.fixture
async def publication_source(published_inputs):
    from sag_api.core.db import init_db
    from sag_api.db.models import Source

    source_ids = []

    async def create(name: str, config_id: str, *input_ids: str):
        await init_db()
        async with db.SessionLocal() as session:
            source = Source(name=name, sag_source_config_id=config_id, config={})
            session.add(source)
            await session.commit()
            source_ids.append(source.id)
        await published_inputs(source.id, *input_ids)
        return source

    yield create
    if source_ids:
        async with db.SessionLocal() as session:
            await session.execute(delete(Source).where(Source.id.in_(source_ids)))
            await session.commit()
