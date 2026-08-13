from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from octx import open_octx, validate_octx

from sag_api.sag.octx_vector_protocol import (
    ROLE_TARGETS,
    input_sha256,
    render_recipe_input,
    vector_profile,
)


def _create_cache(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS vector_reuse_locations (
            role TEXT NOT NULL,
            record_id TEXT NOT NULL,
            batch_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            input_sha256 TEXT NOT NULL,
            PRIMARY KEY(role, record_id)
        );
        CREATE TABLE IF NOT EXISTS vector_reuse_sources (
            role TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            arrow_path TEXT NOT NULL,
            dimension INTEGER NOT NULL
        );
        """
    )


def _payload(connection: sqlite3.Connection, kind: str, record_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload FROM records WHERE kind = ? AND octx_id = ?",
        (kind, record_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"OCTX vector references unknown {kind}: {record_id}")
    return dict(json.loads(row[0]))


def _relation_payload(connection: sqlite3.Connection, record_id: str) -> dict[str, Any]:
    parts = record_id.split(":")
    if len(parts) != 3 or parts[0] != "event_entity":
        raise ValueError(f"invalid OCTX event-entity vector ID: {record_id}")
    event_id, entity_id = parts[1], parts[2]
    row = connection.execute(
        """
        SELECT payload FROM relations
        WHERE kind = 'event_entity' AND left_id = ? AND right_id = ?
        """,
        (event_id, entity_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"OCTX vector references unknown event-entity relation: {record_id}")
    relation = dict(json.loads(row[0]))
    relation["event"] = _payload(connection, "event", event_id)
    relation["entity"] = _payload(connection, "entity", entity_id)
    return relation


def _input_record(connection: sqlite3.Connection, role: str, record_id: str) -> dict[str, Any]:
    if role.startswith("chunk."):
        return _payload(connection, "chunk", record_id)
    if role.startswith("event."):
        return _payload(connection, "event", record_id)
    if role == "entity.name":
        return _payload(connection, "entity", record_id)
    if role == "event_entity.relation":
        return _relation_payload(connection, record_id)
    raise ValueError(f"unknown OCTX vector role: {role}")


def _extract_payload(package: Any, logical_path: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_suffix(output_path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    try:
        with package.open_payload(logical_path) as source, partial.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(output_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _index_role(
    connection: sqlite3.Connection,
    *,
    role: str,
    profile: dict[str, Any],
    arrow_path: Path,
) -> bool:
    import pyarrow as pa
    import pyarrow.ipc as ipc

    connection.execute("DELETE FROM vector_reuse_locations WHERE role = ?", (role,))
    try:
        with pa.memory_map(str(arrow_path), "r") as source:
            reader = ipc.open_file(source)
            vector_index = reader.schema.get_field_index("vector")
            record_index = reader.schema.get_field_index("record_id")
            hash_index = reader.schema.get_field_index("input_sha256")
            vector_type = reader.schema.field(vector_index).type
            if not pa.types.is_fixed_size_list(vector_type) or not pa.types.is_float32(vector_type.value_type):
                return False
            if vector_type.list_size != int(profile["dimensions"]):
                return False
            for batch_index in range(reader.num_record_batches):
                batch = reader.get_batch(batch_index)
                record_ids = batch.column(record_index).to_pylist()
                input_hashes = batch.column(hash_index).to_pylist()
                for row_index, (raw_record_id, raw_input_hash) in enumerate(
                    zip(record_ids, input_hashes, strict=True)
                ):
                    record_id = str(raw_record_id)
                    rendered = render_recipe_input(
                        dict(profile["recipe"]),
                        _input_record(connection, role, record_id),
                    )
                    expected_hash = input_sha256(rendered)
                    if expected_hash != raw_input_hash:
                        return False
                    connection.execute(
                        """
                        INSERT INTO vector_reuse_locations(
                            role, record_id, batch_index, row_index, input_sha256
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (role, record_id, batch_index, row_index, expected_hash),
                    )
    except (OSError, ValueError, sqlite3.IntegrityError):
        return False
    return True


def prepare_vector_reuse(
    package_path: str | Path,
    plan_path: str | Path,
    embedding_client: Any,
    *,
    prevalidated_vector_valid: bool | None = None,
) -> set[str]:
    if prevalidated_vector_valid is False:
        return set()
    if prevalidated_vector_valid is None:
        report = validate_octx(package_path)
        vector_layer = report.capabilities.get("vectors")
        if vector_layer is None or vector_layer.version != "0.1" or vector_layer.valid is not True:
            return set()

    reusable: set[str] = set()
    plan = Path(plan_path)
    reuse_root = plan.parent / "vector-reuse"
    with open_octx(package_path, validate=False) as package, sqlite3.connect(plan) as connection:
        declaration = package.manifest.get("capabilities", {}).get("vectors")
        version = declaration.get("version") if isinstance(declaration, dict) else declaration
        if version != "0.1":
            return set()
        _create_cache(connection)
        connection.execute("DELETE FROM vector_reuse_locations")
        connection.execute("DELETE FROM vector_reuse_sources")
        profiles_document = json.loads(package.read_payload("vectors/profiles.json"))
        for profile in profiles_document["profiles"]:
            role = str(profile["role"])
            dimensions = int(profile["dimensions"])
            local_profile = vector_profile(role, embedding_client, dimensions)
            if local_profile["fingerprint"] != profile["fingerprint"]:
                continue
            if profile.get("coverage") != "complete":
                continue
            target = ROLE_TARGETS[role]
            logical_path = f"vectors/{target}.arrow"
            output_path = reuse_root / f"{target}.arrow"
            try:
                _extract_payload(package, logical_path, output_path)
                role_valid = _index_role(
                    connection,
                    role=role,
                    profile=dict(profile),
                    arrow_path=output_path,
                )
            except (KeyError, OSError, ValueError):
                role_valid = False
            if not role_valid:
                connection.execute("DELETE FROM vector_reuse_locations WHERE role = ?", (role,))
                output_path.unlink(missing_ok=True)
                continue
            connection.execute(
                """
                INSERT INTO vector_reuse_sources(role, fingerprint, arrow_path, dimension)
                VALUES (?, ?, ?, ?)
                """,
                (role, profile["fingerprint"], str(output_path), dimensions),
            )
            reusable.add(role)
        connection.commit()
    return reusable


class ArrowVectorReuseReader:
    """Batch reader for validated OCTX vectors backed by memory-mapped Arrow files."""

    def __init__(self, plan_path: str | Path) -> None:
        self._plan_path = Path(plan_path)
        with sqlite3.connect(self._plan_path) as connection:
            _create_cache(connection)

    def __enter__(self) -> ArrowVectorReuseReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get_many(self, role: str, record_ids: Sequence[str]) -> dict[str, list[float]]:
        if not record_ids:
            return {}
        locations: list[tuple[str, int, int]] = []
        with sqlite3.connect(self._plan_path) as connection:
            source_row = connection.execute(
                "SELECT arrow_path FROM vector_reuse_sources WHERE role = ?", (role,)
            ).fetchone()
            if source_row is None:
                return {}
            arrow_path = str(source_row[0])
            for offset in range(0, len(record_ids), 900):
                part = list(record_ids[offset : offset + 900])
                placeholders = ",".join("?" for _ in part)
                locations.extend(
                    (str(record_id), int(batch_index), int(row_index))
                    for record_id, batch_index, row_index in connection.execute(
                        f"""
                        SELECT record_id, batch_index, row_index
                        FROM vector_reuse_locations
                        WHERE role = ? AND record_id IN ({placeholders})
                        """,  # noqa: S608
                        (role, *part),
                    )
                )
        grouped: dict[int, list[tuple[str, int]]] = {}
        for record_id, batch_index, row_index in locations:
            grouped.setdefault(batch_index, []).append((record_id, row_index))
        vectors: dict[str, list[float]] = {}
        import pyarrow as pa
        import pyarrow.ipc as ipc

        with pa.memory_map(arrow_path, "r") as source:
            reader = ipc.open_file(source)
            for batch_index, rows in grouped.items():
                batch = reader.get_batch(batch_index)
                vector_column = batch.column(batch.schema.get_field_index("vector"))
                for record_id, row_index in rows:
                    vectors[record_id] = [float(value) for value in vector_column[row_index].as_py()]
        return vectors

    def close(self) -> None:
        return


def get_reused_vector(plan_path: str | Path, role: str, record_id: str) -> list[float] | None:
    with ArrowVectorReuseReader(plan_path) as reader:
        return reader.get_many(role, [record_id]).get(record_id)


def reusable_roles(plan_path: str | Path) -> set[str]:
    with sqlite3.connect(plan_path) as connection:
        _create_cache(connection)
        rows = connection.execute("SELECT role FROM vector_reuse_sources")
        return {str(row[0]) for row in rows}
