from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from zleap.sag import DataEngine

from sag_api.core.config import Settings
from sag_api.sag.config_builder import build_engine_config
from sag_api.upgrades.types import StorageUpgradeError

_INSERT_BATCH_SIZE = 1_000


@dataclass(frozen=True)
class GenerationIdentity:
    generation_id: str
    source_version: str
    chunk_version: str


@dataclass(frozen=True)
class RelationalMigrationReport:
    generation_by_source: dict[tuple[str, str], GenerationIdentity]
    source_by_event: dict[str, tuple[str, str]]
    row_counts: dict[str, int]


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_rows(db: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not exists:
        return []
    cursor = db.execute(f'SELECT * FROM "{table}"')
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _generation_identities(
    rows: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, str], GenerationIdentity]:
    chunks_by_source: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    events_by_source: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    source_type_by_key: dict[tuple[str, str], str] = {}
    for row in rows["source_chunk"]:
        key = (str(row["source_config_id"]), str(row["source_id"]))
        chunks_by_source[key].append(row)
        source_type_by_key[key] = str(row["source_type"])
    for row in rows["source_event"]:
        key = (str(row["source_config_id"]), str(row["source_id"]))
        events_by_source[key].append(row)
        source_type_by_key.setdefault(key, str(row["source_type"]))
    for row in rows["article"]:
        key = (str(row["source_config_id"]), str(row["id"]))
        source_type_by_key.setdefault(key, "ARTICLE")

    identities: dict[tuple[str, str], GenerationIdentity] = {}
    for key in sorted(source_type_by_key):
        chunk_rows = sorted(chunks_by_source[key], key=lambda item: str(item["id"]))
        event_rows = sorted(events_by_source[key], key=lambda item: str(item["id"]))
        source_version = _canonical_digest({"chunks": chunk_rows, "events": event_rows})
        chunk_version = _canonical_digest([str(row["id"]) for row in chunk_rows])
        identities[key] = GenerationIdentity(
            generation_id=str(uuid5(NAMESPACE_URL, f"sag-upgrade:{key[0]}:{key[1]}:{source_version}")),
            source_version=source_version,
            chunk_version=chunk_version,
        )
    return identities


def _renamed(row: dict[str, Any]) -> dict[str, Any]:
    mapped = dict(row)
    if "source_config_id" in mapped:
        mapped["data_source_id"] = mapped.pop("source_config_id")
    return mapped


def _project(row: dict[str, Any], columns: set[str]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key in columns}


async def _insert_rows(session: Any, table: str, rows: list[dict[str, Any]], columns: set[str]) -> None:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        values = _project(row, columns)
        grouped[tuple(values)].append(values)

    for names, values in grouped.items():
        quoted_names = ", ".join(f'"{name}"' for name in names)
        placeholders = ", ".join(f":{name}" for name in names)
        statement = text(f'INSERT INTO "{table}" ({quoted_names}) VALUES ({placeholders})')
        for offset in range(0, len(values), _INSERT_BATCH_SIZE):
            await session.execute(statement, values[offset : offset + _INSERT_BATCH_SIZE])


async def migrate_relational(
    legacy_db: Path,
    target_data_dir: Path,
    *,
    settings: Settings,
) -> RelationalMigrationReport:
    if not legacy_db.is_file():
        raise StorageUpgradeError(
            "legacy relational database is missing",
            stage="relational",
            recoverable=True,
        )
    if (target_data_dir / "sag.db").exists():
        raise StorageUpgradeError(
            "target relational database must be empty",
            stage="relational",
            recoverable=True,
        )

    with sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True) as legacy:
        legacy.row_factory = sqlite3.Row
        table_names = (
            "source_config",
            "kb_document",
            "article",
            "article_section",
            "chat_conversation",
            "chat_message",
            "entity_type",
            "entity",
            "source_chunk",
            "source_event",
            "event_entity",
        )
        rows = {table: _read_rows(legacy, table) for table in table_names}

    identities = _generation_identities(rows)
    config = build_engine_config(settings, overrides={"data_dir": str(target_data_dir)})
    engine = DataEngine(config, health_check=False)
    try:
        await engine.start()
        relational = engine.resources.relational
        async with relational.engine().connect() as connection:
            target_columns = {
                table: {
                    str(row[1]) for row in (await connection.execute(text(f'PRAGMA table_info("{table}")'))).fetchall()
                }
                for table in (
                    "data_source",
                    "kb_document",
                    "article",
                    "article_section",
                    "chat_conversation",
                    "chat_message",
                    "entity_type",
                    "entity",
                    "source_chunk",
                    "source_event",
                    "event_entity",
                )
            }

        kb_document_ids = {str(row["id"]) for row in rows["kb_document"]}
        converted: dict[str, list[dict[str, Any]]] = {}
        converted["data_source"] = [
            {key: value for key, value in row.items() if key not in {"target_config"}} for row in rows["source_config"]
        ]
        converted["kb_document"] = []
        source_ids = {str(row["id"]) for row in rows["source_config"]}
        for row in rows["kb_document"]:
            item = dict(row)
            candidate = str(item.pop("knowledge_base_id", ""))
            item["data_source_id"] = candidate if candidate in source_ids else candidate
            item.setdefault("doc_type", item.get("file_type") or "DOCUMENT")
            converted["kb_document"].append(item)
        converted["article"] = []
        for row in rows["article"]:
            item = _renamed(row)
            legacy_document_id = item.pop("source_id", None)
            item["document_id"] = (
                legacy_document_id if legacy_document_id and str(legacy_document_id) in kb_document_ids else None
            )
            converted["article"].append(item)
        for table in (
            "article_section",
            "chat_conversation",
            "chat_message",
            "entity_type",
            "entity",
            "source_chunk",
            "source_event",
            "event_entity",
        ):
            converted[table] = [_renamed(row) for row in rows[table]]

        article_source = {str(row["id"]): (str(row["source_config_id"]), str(row["id"])) for row in rows["article"]}
        for item in converted["article_section"]:
            key = article_source.get(str(item["article_id"]))
            if key and key in identities:
                item["generation_id"] = identities[key].generation_id
        for table in ("source_chunk", "source_event"):
            for item in converted[table]:
                key = (str(item["data_source_id"]), str(item["source_id"]))
                item["generation_id"] = identities[key].generation_id

        source_type_by_key: dict[tuple[str, str], str] = {}
        for table in ("source_chunk", "source_event"):
            for row in rows[table]:
                source_type_by_key[(str(row["source_config_id"]), str(row["source_id"]))] = str(row["source_type"])
        for row in rows["article"]:
            source_type_by_key.setdefault((str(row["source_config_id"]), str(row["id"])), "ARTICLE")

        chunk_counts: dict[tuple[str, str], int] = defaultdict(int)
        event_counts: dict[tuple[str, str], int] = defaultdict(int)
        for row in rows["source_chunk"]:
            chunk_counts[(str(row["source_config_id"]), str(row["source_id"]))] += 1
        for row in rows["source_event"]:
            event_counts[(str(row["source_config_id"]), str(row["source_id"]))] += 1

        async with relational.session() as session, session.begin():
            # Legacy rows are not guaranteed to store a parent event before its
            # children. Defer self-referential FK checks until the full import
            # transaction is present; dangling references still fail at commit.
            await session.execute(text("PRAGMA defer_foreign_keys = ON"))
            await _insert_rows(
                session,
                "data_source",
                converted["data_source"],
                target_columns["data_source"],
            )
            await _insert_rows(
                session,
                "kb_document",
                converted["kb_document"],
                target_columns["kb_document"],
            )
            for table in (
                "article",
                "article_section",
                "chat_conversation",
                "chat_message",
                "entity_type",
                "entity",
                "source_chunk",
                "source_event",
                "event_entity",
            ):
                await _insert_rows(session, table, converted[table], target_columns[table])

            for data_source_id in sorted(source_ids):
                await session.execute(
                    text("INSERT INTO sag_data_source_manifest (data_source_id, revision, active) VALUES (:id, 1, 1)"),
                    {"id": data_source_id},
                )
            for (data_source_id, source_id), identity in identities.items():
                await session.execute(
                    text(
                        "INSERT INTO sag_source_manifest "
                        "(data_source_id,source_id,source_type,active_generation_id,"
                        "operation_id,fence_scope,fence_token,status,source_version,"
                        "chunk_version,extract_version,chunk_count,event_count,revision,"
                        "pending_cleanup_generations,cleanup_required) "
                        "VALUES (:data_source_id,:source_id,:source_type,:generation_id,"
                        ":operation_id,:fence_scope,1,'ready',:source_version,"
                        ":chunk_version,'legacy-0.7.1',:chunk_count,:event_count,1,'[]',0)"
                    ),
                    {
                        "data_source_id": data_source_id,
                        "source_id": source_id,
                        "source_type": source_type_by_key[(data_source_id, source_id)],
                        "generation_id": identity.generation_id,
                        "operation_id": f"upgrade:{identity.generation_id}",
                        "fence_scope": f"source:{data_source_id}:{source_id}",
                        "source_version": identity.source_version,
                        "chunk_version": identity.chunk_version,
                        "chunk_count": chunk_counts[(data_source_id, source_id)],
                        "event_count": event_counts[(data_source_id, source_id)],
                    },
                )
    except StorageUpgradeError:
        raise
    except Exception as error:
        raise StorageUpgradeError(
            f"legacy relational migration failed: {error}",
            stage="relational",
            recoverable=True,
        ) from error
    finally:
        await engine.aclose()

    return RelationalMigrationReport(
        generation_by_source=identities,
        source_by_event={
            str(row["id"]): (str(row["source_config_id"]), str(row["source_id"])) for row in rows["source_event"]
        },
        row_counts={table: len(items) for table, items in rows.items()},
    )
