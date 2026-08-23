from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import lancedb
from zleap.sag import DataEngine
from zleap.sag.core.adapters.models import Filter, VectorQuery

from sag_api.upgrades.types import StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.relational import (
    RelationalMigrationReport,
)
from sag_api.upgrades.zleap_sag_0_7_to_0_8.vectors import VectorMigrationReport


@dataclass(frozen=True)
class VerificationReport:
    relational_counts: dict[str, int]
    vector_counts: dict[str, int]
    sampled_data_sources: tuple[str, ...]


RELATIONAL_TABLES = {
    "source_config": "data_source",
    "article": "article",
    "source_chunk": "source_chunk",
    "source_event": "source_event",
    "entity": "entity",
    "event_entity": "event_entity",
}


def _count(db: sqlite3.Connection, table: str) -> int:
    return int(db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])


def _vector_rows(
    path: Path,
    table: str,
    columns: list[str],
    *,
    batch_size: int = 10_000,
) -> Iterator[dict[str, object]]:
    source = lancedb.connect(path).open_table(table)
    reader = source.search(None).select(columns).to_batches(batch_size=batch_size)
    for batch in reader:
        yield from batch.to_pylist()


def _vector_ids(path: Path, table: str) -> set[str]:
    return {str(row["id"]) for row in _vector_rows(path, table, ["id"])}


def _legacy_chunk_probes(path: Path) -> dict[str, list[float]]:
    probes: dict[str, list[float]] = {}
    for row in _vector_rows(
        path,
        "source_chunks",
        ["source_config_id", "content_vector"],
        batch_size=500,
    ):
        data_source_id = row.get("source_config_id")
        vector = row.get("content_vector")
        if data_source_id and vector and str(data_source_id) not in probes:
            probes[str(data_source_id)] = [float(item) for item in vector]
    return probes


async def verify_migration(
    legacy_engine: Path,
    target_engine: Path,
    engine: DataEngine,
    relational_report: RelationalMigrationReport,
    vector_report: VectorMigrationReport,
) -> VerificationReport:
    counts: dict[str, int] = {}
    with sqlite3.connect(f"file:{legacy_engine / 'sag.db'}?mode=ro", uri=True) as old:
        with sqlite3.connect(f"file:{target_engine / 'sag.db'}?mode=ro", uri=True) as new:
            for old_table, new_table in RELATIONAL_TABLES.items():
                old_count = _count(old, old_table)
                new_count = _count(new, new_table)
                if old_count != new_count:
                    raise StorageUpgradeError(
                        f"relational count mismatch {old_table}: {old_count} != {new_count}",
                        stage="verify",
                        recoverable=True,
                    )
                counts[new_table] = new_count
            if new.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageUpgradeError(
                    "migrated relational database has foreign key violations",
                    stage="verify",
                    recoverable=True,
                )
            ready = _count(new, "sag_source_manifest")
            if ready != len(relational_report.generation_by_source):
                raise StorageUpgradeError(
                    "source manifest count does not match migrated sources",
                    stage="verify",
                    recoverable=True,
                )

    table_mapping = {
        "source_chunks": "source_chunks",
        "event_vectors": "event_vectors_wide",
        "entity_vectors": "entity_vectors",
        "event_entity_vectors": "event_entity_vectors",
    }
    for old_table, new_table in table_mapping.items():
        legacy_ids = _vector_ids(legacy_engine / "lancedb", old_table)
        target_ids: set[str] = set()
        target_count = 0
        missing_data_source = False
        for row in _vector_rows(
            target_engine / "lancedb",
            new_table,
            ["id", "data_source_id"],
        ):
            target_ids.add(str(row["id"]))
            target_count += 1
            missing_data_source = missing_data_source or not row.get("data_source_id")
        if target_ids != legacy_ids:
            raise StorageUpgradeError(
                f"vector id mismatch for {new_table}",
                stage="verify",
                recoverable=True,
            )
        if missing_data_source:
            raise StorageUpgradeError(
                f"vector data_source_id missing for {new_table}",
                stage="verify",
                recoverable=True,
            )
        if target_count != vector_report.row_counts[new_table]:
            raise StorageUpgradeError(
                f"vector count mismatch for {new_table}",
                stage="verify",
                recoverable=True,
            )

    sampled: list[str] = []
    probes = _legacy_chunk_probes(legacy_engine / "lancedb")
    for data_source_id, probe_vector in sorted(probes.items()):
        hits = await engine.resources.vector.query(
            "source_chunks",
            VectorQuery(
                vector=probe_vector,
                vector_field="content_vector",
                filters=Filter.eq("data_source_id", data_source_id),
                limit=1,
            ),
        )
        if not hits or hits[0].payload.get("data_source_id") != data_source_id:
            raise StorageUpgradeError(
                f"migrated vector query is not source-isolated for {data_source_id}",
                stage="verify",
                recoverable=True,
            )
        sampled.append(data_source_id)
    return VerificationReport(counts, vector_report.row_counts, tuple(sampled))
