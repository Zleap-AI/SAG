from __future__ import annotations

import sqlite3
from pathlib import Path

import lancedb

from sag_api.core.config import Settings
from sag_api.upgrades.types import StorageLayout, StorageProbe, StorageVersion


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def _relational_probe(db_path: Path) -> tuple[dict[str, set[str]], str | None]:
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {table: _table_columns(db, table) for table in tables}
        schema_version: str | None = None
        if "sag_schema_meta" in tables:
            row = db.execute("SELECT schema_version FROM sag_schema_meta ORDER BY id LIMIT 1").fetchone()
            schema_version = str(row[0]) if row else None
        return columns, schema_version


def _vector_tables(lance_path: Path) -> set[str]:
    if not lance_path.is_dir():
        return set()
    database = lancedb.connect(lance_path)
    result = database.list_tables()
    names = getattr(result, "tables", result)
    return {str(getattr(item, "name", item)) for item in names}


def detect_storage(layout: StorageLayout, settings: Settings) -> StorageProbe:
    if settings.sag_vector_provider != "lancedb" or settings.sag_relational_provider not in (
        None,
        "sqlite",
    ):
        return StorageProbe(
            StorageVersion.UNSUPPORTED,
            "automatic storage upgrades support only SQLite plus LanceDB",
        )

    if not layout.engine.exists():
        return StorageProbe(StorageVersion.EMPTY, "engine directory does not exist")
    if not layout.engine.is_dir():
        return StorageProbe(StorageVersion.UNKNOWN, "engine path is not a directory")
    if not any(layout.engine.iterdir()):
        return StorageProbe(StorageVersion.EMPTY, "engine directory is empty")

    db_path = layout.engine / "sag.db"
    if not db_path.is_file():
        return StorageProbe(StorageVersion.UNKNOWN, "engine database is missing")
    try:
        columns, schema_version = _relational_probe(db_path)
        vectors = _vector_tables(layout.engine / "lancedb")
    except Exception as error:
        return StorageProbe(StorageVersion.UNKNOWN, f"storage cannot be inspected: {error}")

    article_columns = columns.get("article", set())
    if "sag_schema_meta" in columns and "data_source" in columns and "data_source_id" in article_columns:
        return StorageProbe(
            StorageVersion.CURRENT,
            "zleap-sag schema metadata and current columns are present",
            columns,
            vectors,
            schema_version,
        )
    if (
        "source_config" in columns
        and "source_config_id" in article_columns
        and "data_source_id" not in article_columns
        and {"source_chunks", "event_vectors"} <= vectors
    ):
        return StorageProbe(
            StorageVersion.LEGACY_0_7,
            "legacy 0.7 relational and vector contracts are present",
            columns,
            vectors,
            schema_version,
        )
    return StorageProbe(
        StorageVersion.UNKNOWN,
        "storage does not match a supported complete schema",
        columns,
        vectors,
        schema_version,
    )
