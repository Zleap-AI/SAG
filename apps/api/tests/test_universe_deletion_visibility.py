"""Cached Universe presentation applies the same logical deletion barrier."""

from uuid import uuid4

import pytest
from sqlalchemy import delete

from sag_api.core.config import settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.db.models import Source, UniverseOverview, UniversePartition, User
from sag_api.enums import SourceStatus
from sag_api.services.universe_query import universe_manifest


@pytest.mark.parametrize("status", [SourceStatus.DELETING, SourceStatus.DELETE_FAILED])
async def test_cached_manifest_filters_before_limit_and_counts_all_visible_partitions(monkeypatch, status):
    await init_db()
    monkeypatch.setattr(settings, "universe_manifest_source_limit", 1)
    async with SessionLocal() as session:
        sources = [Source(name="paused", status=SourceStatus.PAUSED, sag_source_config_id=uuid4().hex),
                   Source(name="error", status=SourceStatus.ERROR, sag_source_config_id=uuid4().hex),
                   Source(name="deleting", status=status, sag_source_config_id=uuid4().hex)]
        user = User(email=f"universe-delete-{uuid4().hex}@test", password_hash="unused")
        session.add_all([*sources, user])
        await session.flush()
        overview = UniverseOverview(
            user_id=user.id, schema_version=4, is_active=True, status="ready", source_count=3,
            event_count=112, entity_count=105, node_count=217, relation_count=109,
        )
        session.add(overview)
        await session.flush()
        for source, events, entities, nodes, relations, importance in [
            (sources[0], 5, 2, 7, 4, 2), (sources[1], 7, 3, 10, 5, 1), (sources[2], 100, 100, 200, 100, 999),
        ]:
            session.add(UniversePartition(
                overview_id=overview.id, user_id=user.id, source_id=source.id, kind="source", key=source.id,
                label=source.name, x=12, y=34, event_count=events, entity_count=entities,
                node_count=nodes, relation_count=relations, importance=importance,
            ))
        await session.commit()
        try:
            manifest = await universe_manifest(session, user_id=user.id)
            assert [partition.source_id for partition in manifest["partitions"]] == [sources[0].id]
            assert manifest["counts"] == {
                "sources": 2, "partitions": 1, "events": 12, "entities": 5, "nodes": 17, "relations": 9,
            }
            assert (manifest["partitions"][0].x, manifest["partitions"][0].y) == (12, 34)
            sources[0].status = SourceStatus.DELETING
            sources[1].status = SourceStatus.DELETE_FAILED
            await session.commit()
            empty = await universe_manifest(session, user_id=user.id)
            assert empty["partitions"] == []
            assert empty["counts"] == {
                "sources": 0, "partitions": 0, "events": 0, "entities": 0, "nodes": 0, "relations": 0,
            }
        finally:
            await session.execute(delete(User).where(User.id == user.id))
            await session.execute(delete(Source).where(Source.id.in_([source.id for source in sources])))
            await session.commit()
