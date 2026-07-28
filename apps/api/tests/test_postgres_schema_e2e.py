from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import func, inspect, select, text

pytestmark = pytest.mark.skipif(
    os.getenv("SAG_E2E_POSTGRES") != "1",
    reason="requires the dedicated PostgreSQL E2E environment",
)


async def _table_names(async_engine) -> set[str]:
    async with async_engine.connect() as connection:
        return await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))


@pytest.mark.asyncio
async def test_fresh_postgres_bootstraps_engine_schema_before_first_source():
    from sag_api.core.db import SessionLocal
    from sag_api.core.db import engine as application_engine
    from sag_api.db.models import Source
    from sag_api.main import app

    async with application_engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    assert "source_config" not in await _table_names(application_engine)

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            register = await client.post(
                "/api/v1/auth/register",
                json={"email": "postgres-e2e@example.com", "password": "password123"},
            )
            assert register.status_code == 201, register.text
            headers = {"Authorization": f"Bearer {register.json()['access_token']}"}
            created = await client.post(
                "/api/v1/sources",
                headers=headers,
                json={"name": "PostgreSQL 首个信源"},
            )
            assert created.status_code == 201, created.text

        async with SessionLocal() as session:
            source = await session.get(Source, created.json()["id"])
            assert source is not None
            source_config_id = source.sag_source_config_id

        # Before the fix this call fails with PostgreSQL UndefinedTable:
        # relation "source_config" does not exist.
        await app.state.engine_manager.provision(source_config_id, source)

        from zleap.sag.db import get_engine, get_session_factory
        from zleap.sag.db.models import EntityType, SourceConfig

        engine_tables = await _table_names(get_engine())
        assert {"source_config", "source_chunk", "source_event"} <= engine_tables
        async with get_session_factory()() as session:
            parent = await session.get(SourceConfig, source_config_id)
            entity_type_count = await session.scalar(
                select(func.count()).select_from(EntityType).where(EntityType.scope == "global")
            )
        assert parent is not None
        assert parent.name == "PostgreSQL 首个信源"
        assert entity_type_count is not None and entity_type_count > 0

    # A new manager lifecycle must safely repeat the idempotent bootstrap.
    async with app.router.lifespan_context(app):
        async with SessionLocal() as session:
            source = await session.get(Source, created.json()["id"])
            assert source is not None
        await app.state.engine_manager.provision(source.sag_source_config_id, source)
