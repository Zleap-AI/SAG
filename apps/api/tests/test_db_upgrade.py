from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from sag_api.core import db


@pytest.mark.asyncio
async def test_existing_sqlite_database_receives_idempotent_nas_schema(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE documents (
                    id VARCHAR(32) PRIMARY KEY,
                    source_id VARCHAR(32) NOT NULL,
                    sag_source_id VARCHAR(64)
                )
                """
            )
        )
    monkeypatch.setattr(db, "engine", engine)

    await db.init_db()
    await db.init_db()

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {column["name"] for column in inspect(sync).get_columns("documents")}
        )
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        indexes = await connection.run_sync(
            lambda sync: {index["name"]: index for index in inspect(sync).get_indexes("documents")}
        )

    assert {
        "origin_kind",
        "origin_key",
        "origin_path",
        "origin_display_path",
        "origin_size_bytes",
        "origin_mtime_ns",
        "origin_sha256",
    } <= columns
    assert "fnos_nas_legacy_folders" in tables
    assert indexes["ux_documents_source_origin"]["unique"] == 1

    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO documents (id, source_id, origin_kind, origin_key)
                VALUES ('upload-1', 'source', NULL, NULL), ('upload-2', 'source', NULL, NULL),
                       ('nas-1', 'source', 'fnos_shared', :origin_key)
                """
            ),
            {"origin_key": "a" * 64},
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO documents (id, source_id, origin_kind, origin_key)
                    VALUES ('nas-2', 'source', 'fnos_shared', :origin_key)
                    """
                ),
                {"origin_key": "a" * 64},
            )

    await engine.dispose()
