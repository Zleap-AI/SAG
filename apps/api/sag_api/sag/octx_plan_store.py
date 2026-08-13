from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from sag_api.sag.octx_ids import installation_local_id, named_local_id, relation_local_id

_RECORD_KINDS = frozenset(
    {"article", "chunk", "document", "entity", "entity_type", "event"}
)
_RELATION_FIELDS = {
    "chunk_event": ("chunk_id", "event_id", "chunk", "event"),
    "event_entity": ("event_id", "entity_id", "event", "entity"),
}


class OctxPlanError(ValueError):
    pass


class OctxPlanStore:
    """Bounded on-disk import plan and OCTX-to-local identity index."""

    def __init__(
        self,
        path: str | Path,
        namespace: uuid.UUID | str,
        *,
        create: bool = True,
    ) -> None:
        self.path = Path(path)
        self.namespace = uuid.UUID(str(namespace))
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise FileNotFoundError(self.path)
        self._connection = sqlite3.connect(self.path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if create:
            self._create_schema()
        self._verify_namespace()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                octx_id TEXT NOT NULL,
                local_id TEXT NOT NULL UNIQUE,
                document_id TEXT,
                ordinal INTEGER,
                type_key TEXT,
                normalized_name TEXT,
                payload TEXT NOT NULL,
                UNIQUE(kind, octx_id)
            );
            CREATE INDEX IF NOT EXISTS ix_octx_plan_record_document
                ON records(kind, document_id, ordinal);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_octx_plan_entity_identity
                ON records(type_key, normalized_name)
                WHERE kind = 'entity';
            CREATE TABLE IF NOT EXISTS entity_types (
                type_key TEXT PRIMARY KEY,
                local_id TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS relations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                left_id TEXT NOT NULL,
                right_id TEXT NOT NULL,
                local_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                UNIQUE(kind, left_id, right_id)
            );
            CREATE INDEX IF NOT EXISTS ix_octx_plan_relation_right
                ON relations(kind, right_id, left_id);
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES ('namespace', ?)",
            (str(self.namespace),),
        )
        self._connection.commit()

    def _verify_namespace(self) -> None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'namespace'"
        ).fetchone()
        if row is None or row["value"] != str(self.namespace):
            self.close()
            raise OctxPlanError("plan store installation namespace does not match")

    def __enter__(self) -> OctxPlanStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_connection", None) is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def add_record(self, kind: str, payload: Mapping[str, Any]) -> str:
        kind = kind.strip().casefold()
        if kind not in _RECORD_KINDS:
            raise OctxPlanError(f"unsupported record kind: {kind}")
        octx_id = payload.get("id")
        if not isinstance(octx_id, str):
            raise OctxPlanError(f"{kind} record is missing string id")
        local_id = installation_local_id(self.namespace, kind, octx_id)
        document_id = payload.get("document_id")
        ordinal = payload.get("ordinal")
        type_key = payload.get("type") if kind == "entity" else None
        name = payload.get("name") if kind == "entity" else None
        if kind == "entity" and (not isinstance(type_key, str) or not isinstance(name, str)):
            raise OctxPlanError("entity record requires type and name")
        normalized_name = (
            unicodedata.normalize("NFC", name).casefold() if isinstance(name, str) else None
        )
        try:
            self._connection.execute(
                """
                INSERT INTO records(
                    kind, octx_id, local_id, document_id, ordinal,
                    type_key, normalized_name, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    octx_id,
                    local_id,
                    document_id if isinstance(document_id, str) else None,
                    ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None,
                    type_key,
                    normalized_name,
                    self._json(payload),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise OctxPlanError(f"duplicate {kind} identity: {octx_id}") from error
        return local_id

    def ensure_entity_type(self, type_key: str) -> str:
        normalized = type_key.strip().casefold()
        if not normalized:
            raise OctxPlanError("entity type must not be empty")
        local_id = named_local_id(self.namespace, "entity_type", normalized)
        self._connection.execute(
            "INSERT OR IGNORE INTO entity_types(type_key, local_id) VALUES (?, ?)",
            (normalized, local_id),
        )
        self._connection.commit()
        return local_id

    def iter_entity_types(self) -> Iterator[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT type_key, local_id FROM entity_types ORDER BY type_key"
        )
        for row in rows:
            yield str(row["type_key"]), str(row["local_id"])

    def _require_record(self, kind: str, octx_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM records WHERE kind = ? AND octx_id = ?",
            (kind, octx_id),
        ).fetchone()
        if row is None:
            raise OctxPlanError(f"relation references unknown {kind}: {octx_id}")

    def add_relation(self, kind: str, payload: Mapping[str, Any]) -> str:
        kind = kind.strip().casefold()
        fields = _RELATION_FIELDS.get(kind)
        if fields is None:
            raise OctxPlanError(f"unsupported relation kind: {kind}")
        left_field, right_field, left_kind, right_kind = fields
        left_id = payload.get(left_field)
        right_id = payload.get(right_field)
        if not isinstance(left_id, str) or not isinstance(right_id, str):
            raise OctxPlanError(
                f"{kind} relation requires {left_field} and {right_field}"
            )
        self._require_record(left_kind, left_id)
        self._require_record(right_kind, right_id)
        local_id = relation_local_id(self.namespace, kind, left_id, right_id)
        try:
            self._connection.execute(
                """
                INSERT INTO relations(kind, left_id, right_id, local_id, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, left_id, right_id, local_id, self._json(payload)),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise OctxPlanError(
                f"duplicate {kind} relation: {left_id} -> {right_id}"
            ) from error
        return local_id

    def count(self, kind: str) -> int:
        kind = kind.strip().casefold()
        table = "records" if kind in _RECORD_KINDS else "relations"
        row = self._connection.execute(
            f"SELECT COUNT(*) AS value FROM {table} WHERE kind = ?",  # noqa: S608
            (kind,),
        ).fetchone()
        return int(row["value"])

    def local_id(self, kind: str, octx_id: str) -> str:
        row = self._connection.execute(
            "SELECT local_id FROM records WHERE kind = ? AND octx_id = ?",
            (kind.strip().casefold(), octx_id),
        ).fetchone()
        if row is None:
            raise OctxPlanError(f"unknown {kind}: {octx_id}")
        return str(row["local_id"])

    def get_record(self, kind: str, octx_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload FROM records WHERE kind = ? AND octx_id = ?",
            (kind.strip().casefold(), octx_id),
        ).fetchone()
        if row is None:
            raise OctxPlanError(f"unknown {kind}: {octx_id}")
        return dict(json.loads(row["payload"]))

    def iter_records(self, kind: str) -> Iterator[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM records WHERE kind = ? ORDER BY sequence",
            (kind.strip().casefold(),),
        )
        for row in rows:
            yield dict(json.loads(row["payload"]))

    def iter_relations(self, kind: str) -> Iterator[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload FROM relations WHERE kind = ? ORDER BY sequence",
            (kind.strip().casefold(),),
        )
        for row in rows:
            yield dict(json.loads(row["payload"]))

    def chunk_ids_for_event(self, event_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT relation.left_id
            FROM relations AS relation
            JOIN records AS chunk
              ON chunk.kind = 'chunk' AND chunk.octx_id = relation.left_id
            WHERE relation.kind = 'chunk_event' AND relation.right_id = ?
            ORDER BY chunk.ordinal, relation.left_id
            """,
            (event_id,),
        )
        return [str(row["left_id"]) for row in rows]

    def primary_chunk_id(self, event_id: str) -> str:
        chunk_ids = self.chunk_ids_for_event(event_id)
        if not chunk_ids:
            raise OctxPlanError(f"event has no chunk relation: {event_id}")
        return chunk_ids[0]

    def document_counts(self, document_id: str) -> tuple[int, int]:
        chunk_row = self._connection.execute(
            """
            SELECT COUNT(*) AS value
            FROM records
            WHERE kind = 'chunk' AND document_id = ?
            """,
            (document_id,),
        ).fetchone()
        event_row = self._connection.execute(
            """
            SELECT COUNT(DISTINCT relation.right_id) AS value
            FROM relations AS relation
            JOIN records AS chunk
              ON chunk.kind = 'chunk' AND chunk.octx_id = relation.left_id
            WHERE relation.kind = 'chunk_event' AND chunk.document_id = ?
            """,
            (document_id,),
        ).fetchone()
        return int(chunk_row["value"]), int(event_row["value"])
