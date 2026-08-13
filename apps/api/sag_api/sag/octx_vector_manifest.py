from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path


class VectorExportManifest:
    """Disk-backed mapping between SAG vector rows and OCTX vector records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=OFF")
        self._connection.execute("PRAGMA synchronous=OFF")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_records (
                role TEXT NOT NULL,
                local_id TEXT NOT NULL,
                record_id TEXT NOT NULL,
                input_sha256 TEXT NOT NULL,
                PRIMARY KEY (role, local_id),
                UNIQUE (role, record_id)
            ) WITHOUT ROWID
            """
        )

    def __enter__(self) -> VectorExportManifest:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.commit()
        self._connection.close()

    def add(self, role: str, local_id: str, record_id: str, input_hash: str) -> None:
        self._connection.execute(
            "INSERT INTO vector_records(role, local_id, record_id, input_sha256) VALUES (?, ?, ?, ?)",
            (role, local_id, record_id, input_hash),
        )

    def count(self, role: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) FROM vector_records WHERE role = ?",
            (role,),
        ).fetchone()
        return int(row[0]) if row else 0

    def lookup(self, role: str, local_ids: Iterable[str]) -> dict[str, tuple[str, str]]:
        ids = tuple(dict.fromkeys(str(value) for value in local_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"SELECT local_id, record_id, input_sha256 FROM vector_records "
            f"WHERE role = ? AND local_id IN ({placeholders})",
            (role, *ids),
        )
        return {str(local_id): (str(record_id), str(input_hash)) for local_id, record_id, input_hash in rows}

    def iter_batches(self, role: str, *, batch_size: int) -> Iterator[list[tuple[str, str, str]]]:
        cursor = self._connection.execute(
            "SELECT local_id, record_id, input_sha256 FROM vector_records WHERE role = ? ORDER BY local_id",
            (role,),
        )
        while rows := cursor.fetchmany(batch_size):
            yield [(str(local_id), str(record_id), str(input_hash)) for local_id, record_id, input_hash in rows]
