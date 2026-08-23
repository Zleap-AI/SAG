from __future__ import annotations

import gc
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
from zleap.sag import DataEngine
from zleap.sag.core.adapters.models import VectorRecord

from sag_api.upgrades.types import StorageUpgradeError
from sag_api.upgrades.zleap_sag_0_7_to_0_8.relational import (
    RelationalMigrationReport,
)


@dataclass(frozen=True)
class VectorMigrationReport:
    row_counts: dict[str, int]
    vector_dimensions: set[int]


COLLECTIONS = {
    "source_chunks": "source_chunks",
    "event_vectors": "event_vectors_wide",
    "entity_vectors": "entity_vectors",
    "event_entity_vectors": "event_entity_vectors",
}

REQUIRED_VECTORS = {
    "source_chunks": {"heading_vector", "content_vector"},
    "event_vectors": {"title_vector", "content_vector"},
    "entity_vectors": {"vector"},
    "event_entity_vectors": {"vector"},
}

_DEFAULT_BATCH_SIZE = 5_000


def _table_names(database: Any) -> set[str]:
    result = database.list_tables()
    names = getattr(result, "tables", result)
    return {str(getattr(item, "name", item)) for item in names}


def _row_batches(
    table: Any,
    *,
    batch_size: int,
    columns: list[str] | None = None,
) -> Iterator[list[dict[str, Any]]]:
    query = table.search(None)
    if columns is not None:
        query = query.select(columns)
    reader = query.to_batches(batch_size=batch_size)
    pending: list[dict[str, Any]] = []
    for batch in reader:
        pending.extend(batch.to_pylist())
        while len(pending) >= batch_size:
            yield pending[:batch_size]
            pending = pending[batch_size:]
    if pending:
        yield pending


def _last_row_positions(table: Any, *, batch_size: int) -> dict[str, int]:
    positions: dict[str, int] = {}
    offset = 0
    for rows in _row_batches(table, batch_size=batch_size, columns=["id"]):
        for row in rows:
            positions[str(row.get("id") or "")] = offset
            offset += 1
    return positions


def _vectors(row: dict[str, Any]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {}
    for key in tuple(row):
        value = row[key]
        if key == "vector" or key.endswith("_vector"):
            if value is not None:
                values[key] = [float(item) for item in value]
            row.pop(key)
    return values


def _generation_for(payload: dict[str, Any], report: RelationalMigrationReport) -> str | None:
    data_source_id = str(payload.get("data_source_id") or "")
    source_id = payload.get("source_id")
    if source_id is None and payload.get("event_id") is not None:
        event_source = report.source_by_event.get(str(payload["event_id"]))
        if event_source is not None:
            data_source_id, source_id = event_source
            payload["data_source_id"] = data_source_id
            payload["source_id"] = source_id
    if not data_source_id or source_id is None:
        return None
    identity = report.generation_by_source.get((data_source_id, str(source_id)))
    return identity.generation_id if identity else None


def _convert(
    legacy_collection: str,
    row: dict[str, Any],
    report: RelationalMigrationReport,
) -> VectorRecord:
    item = dict(row)
    record_id = str(item.pop("id", None) or item.pop("_id", None) or "")
    if not record_id:
        raise StorageUpgradeError(
            f"legacy vector row in {legacy_collection} has no id",
            stage="vectors",
            recoverable=True,
        )
    vectors = _vectors(item)
    if legacy_collection == "source_chunks" and "heading_vector" not in vectors and vectors.get("content_vector"):
        # Older SAG releases could persist a valid content vector without a
        # heading vector. Reusing it keeps the chunk searchable without making
        # the storage migration depend on an external embedding service.
        vectors["heading_vector"] = list(vectors["content_vector"])
    missing_vectors = sorted(name for name in REQUIRED_VECTORS[legacy_collection] if not vectors.get(name))
    if missing_vectors:
        raise StorageUpgradeError(
            f"legacy vector {record_id} is missing required fields: {', '.join(missing_vectors)}",
            stage="vectors",
            recoverable=True,
        )
    source_config_id = item.pop("source_config_id", None)
    if source_config_id is not None:
        item["data_source_id"] = str(source_config_id)
        if legacy_collection in {"entity_vectors", "event_entity_vectors"}:
            item["source_config_id"] = str(source_config_id)
    generation_id = _generation_for(item, report)
    if legacy_collection in {
        "source_chunks",
        "event_vectors",
        "event_entity_vectors",
    }:
        if generation_id is None:
            raise StorageUpgradeError(
                f"legacy vector {record_id} cannot be mapped to a generation",
                stage="vectors",
                recoverable=True,
            )
        item["generation_id"] = generation_id
    return VectorRecord(id=record_id, payload=item, vectors=vectors)


async def migrate_vectors(
    legacy_lance: Path,
    target_engine: DataEngine,
    relational_report: RelationalMigrationReport,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> VectorMigrationReport:
    if not legacy_lance.is_dir():
        raise StorageUpgradeError(
            "legacy LanceDB directory is missing",
            stage="vectors",
            recoverable=True,
        )
    database = lancedb.connect(legacy_lance)
    available = _table_names(database)
    missing = set(COLLECTIONS) - available
    if missing:
        raise StorageUpgradeError(
            f"legacy vector tables are missing: {', '.join(sorted(missing))}",
            stage="vectors",
            recoverable=True,
        )

    dimensions: set[int] = set()
    counts: dict[str, int] = {}
    for legacy_collection, target_collection in COLLECTIONS.items():
        written = 0
        table = database.open_table(legacy_collection)
        last_positions = _last_row_positions(table, batch_size=batch_size)
        row_position = 0
        for rows in _row_batches(table, batch_size=batch_size):
            records: list[VectorRecord] = []
            for row in rows:
                record_id = str(row.get("id") or row.get("_id") or "")
                if last_positions.get(record_id) == row_position:
                    records.append(_convert(legacy_collection, row, relational_report))
                row_position += 1
            if not records:
                continue
            for record in records:
                dimensions.update(len(vector) for vector in record.vectors.values())
            if len(dimensions) > 1:
                raise StorageUpgradeError(
                    "legacy vectors contain mixed dimensions",
                    stage="vectors",
                    recoverable=True,
                )
            result = await target_engine.resources.vector.upsert(target_collection, records)
            if result.failure_count or result.success_count != len(records):
                failed_ids = ", ".join(item.id for item in result.failed_items[:5])
                raise StorageUpgradeError(
                    f"vector upsert failed for {target_collection}: {failed_ids}",
                    stage="vectors",
                    recoverable=True,
                )
            written += result.success_count
            del records, rows, result
            gc.collect()
        counts[target_collection] = written

    # Do not call 0.8.2 optimize here: on a multi-fragment migration it can
    # publish an empty current version. Keep the unoptimized but complete data;
    # the engine must provide a safe bulk-import/compaction contract first.
    await target_engine.resources.vector.publish(tuple(COLLECTIONS.values()))
    return VectorMigrationReport(row_counts=counts, vector_dimensions=dimensions)
