"""Read-only recognition of verified fnOS layouts after their writers stop."""

import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

UNKNOWN_KNOWLEDGE = "暂时无法确认知识库状态，已停止升级。请保留数据并联系维护者。"

_COMMON_TABLES = {
    "article",
    "article_section",
    "entity",
    "entity_type",
    "event_entity",
    "kb_document",
    "source_chunk",
    "source_event",
}
_CURRENT_TABLES = _COMMON_TABLES | {
    "data_source",
    "chat_conversation",
    "chat_message",
    "sag_schema_meta",
    "sag_operation",
    "sag_fence",
    "sag_data_source_manifest",
    "sag_source_manifest",
    "sag_search_session",
}
_LEGACY_TABLES = _COMMON_TABLES | {"source_config", "event_entity_embedding"}


def committed_marker(value: object) -> bool:
    return isinstance(value, dict) and value.get("engine") == "0.13.0" and value.get("knowledge_reset") is True


def _copy_sqlite_file(source: Path, target: Path) -> None:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(UNKNOWN_KNOWLEDGE)
        with target.open("wb") as destination:
            shutil.copyfileobj(stream, destination)


@contextmanager
def readonly_sqlite(database: Path):
    """Read a cold database and committed WAL without touching source sidecars.

    Callers must stop all writers before entering and keep them stopped until
    this context exits. Sequential file copies are not a live SQLite snapshot:
    a concurrent commit or checkpoint can mix different database/WAL states.
    Opening only the private copy also prevents SQLite from creating or
    changing SHM in a stopped or crash-recovered source workspace.
    """
    wal = database.with_name(database.name + "-wal")
    if wal.is_symlink():
        raise ValueError(UNKNOWN_KNOWLEDGE)
    with database.open("rb") as stream:
        stream.seek(18)
        rollback_mode = stream.read(2) == b"\x01\x01"
    # WAL-mode headers can create empty WAL/SHM even after a full checkpoint.
    if not wal.exists() and rollback_mode:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            yield connection
        return
    with TemporaryDirectory(prefix="sag-knowledge-read-") as temporary:
        copy = Path(temporary) / database.name
        for suffix in ("", "-wal", "-shm"):
            source = database.with_name(database.name + suffix)
            if not suffix or source.exists() or source.is_symlink():
                _copy_sqlite_file(source, copy.with_name(copy.name + suffix))
        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)) as connection:
            yield connection


def engine_format(database: Path) -> str:
    """Recognize verified 0.13/0.15 normal schema4 or the old 0.7 schema.

    This does not migrate or open a Lance writer. Unknown layouts, empty files
    and corrupt SQLite must never become permission to clear knowledge.
    """
    if database.is_symlink() or not database.is_file():
        raise ValueError(UNKNOWN_KNOWLEDGE)
    try:
        with database.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ValueError(UNKNOWN_KNOWLEDGE)
        with readonly_sqlite(database) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(UNKNOWN_KNOWLEDGE)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

            def columns(table: str) -> set[str]:
                return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}

            if _CURRENT_TABLES <= tables:
                marker = connection.execute("SELECT id, storage_mode, schema_version FROM sag_schema_meta").fetchall()
                if (
                    marker == [(1, "normal", 4)]
                    and all(
                        {"id", "data_source_id", "source_id", "generation_id", "conversation_id"} <= columns(table)
                        and "source_config_id" not in columns(table)
                        for table in ("source_chunk", "source_event")
                    )
                    and {"id", "name", "description"} <= columns("data_source")
                ):
                    return "compatible"
            if (
                tables == _LEGACY_TABLES
                and all(
                    {"id", "source_config_id"} <= columns(table) and "data_source_id" not in columns(table)
                    for table in ("article", "entity", "entity_type", "source_chunk", "source_event")
                )
                and {"id", "target_config"} <= columns("source_config")
                and {"id", "vec"} <= columns("event_entity_embedding")
            ):
                return "legacy"
    except (OSError, sqlite3.Error):
        raise ValueError(UNKNOWN_KNOWLEDGE) from None
    raise ValueError(UNKNOWN_KNOWLEDGE)


def classify_workspace(workspace: Path, marker: object, *, has_sources: bool, has_documents: bool = False) -> str:
    """Classify without using a missing app marker as evidence of incompatibility."""
    current = workspace / "engine-v0.13-clean"
    legacy = workspace / "engine"
    if workspace.is_symlink() or current.is_symlink() or legacy.is_symlink():
        raise ValueError(UNKNOWN_KNOWLEDGE)
    if marker is not None and not isinstance(marker, dict):
        raise ValueError(UNKNOWN_KNOWLEDGE)
    # Existing current data takes precedence over any app marker and over the
    # intentionally retained old engine. A committed marker cannot hide damage.
    if (current / "sag.db").exists():
        if engine_format(current / "sag.db") != "compatible":
            raise ValueError(UNKNOWN_KNOWLEDGE)
        return "compatible"
    if current.exists() and (not current.is_dir() or any(current.iterdir())):
        raise ValueError(UNKNOWN_KNOWLEDGE)
    if committed_marker(marker) and has_documents:
        # A partial restore must not pair old document registrations with a
        # fresh empty index merely because a migration marker survived.
        raise ValueError(UNKNOWN_KNOWLEDGE)
    if (legacy / "sag.db").exists():
        if engine_format(legacy / "sag.db") != "legacy":
            raise ValueError(UNKNOWN_KNOWLEDGE)
        # A reset can have committed before a new tenant's first engine opens.
        return "compatible" if committed_marker(marker) else "legacy"
    if legacy.exists() and (not legacy.is_dir() or any(legacy.iterdir())):
        raise ValueError(UNKNOWN_KNOWLEDGE)
    if committed_marker(marker):
        return "compatible"
    if not has_sources and marker is None:
        return "fresh"
    raise ValueError(UNKNOWN_KNOWLEDGE)


def classify_users(root: Path) -> str:
    """Classify the fixed package users tree without importing its old payload."""
    root = Path(root)
    pkgvar = Path(os.environ["TRIM_PKGVAR"]).resolve(strict=True)
    if root.is_symlink() or not root.is_dir() or root.resolve() != pkgvar / "users":
        raise ValueError(UNKNOWN_KNOWLEDGE)
    states = []
    for user in root.iterdir():
        if (
            not re.fullmatch(r"[1-9][0-9]*(?:-[a-zA-Z0-9_-]{0,16}-[0-9a-f]{8})?", user.name)
            or user.is_symlink()
            or not user.is_dir()
            or any(path.is_symlink() for path in user.rglob("*"))
        ):
            raise ValueError(UNKNOWN_KNOWLEDGE)
        states.append(classify_tenant(user))
    return "legacy" if "legacy" in states else "compatible" if "compatible" in states else "fresh"


def classify_tenant(workspace: Path) -> str:
    """Read a single trusted tenant path without creating a database or index."""
    workspace = Path(workspace)
    if workspace.is_symlink() or any(path.is_symlink() for path in workspace.rglob("*")):
        raise ValueError(UNKNOWN_KNOWLEDGE)
    if not workspace.exists():
        return "fresh"
    if not workspace.is_dir():
        raise ValueError(UNKNOWN_KNOWLEDGE)
    database = workspace / "meta/sag.db"
    if not database.is_file():
        # An uninitialized worker may have created empty private directories.
        # Any regular or special files without metadata are an unknown workspace.
        if any(not path.is_dir() for path in workspace.rglob("*")):
            raise ValueError(UNKNOWN_KNOWLEDGE)
        return "fresh"
    try:
        with readonly_sqlite(database) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError(UNKNOWN_KNOWLEDGE)
            row = connection.execute(
                "SELECT value_json FROM settings WHERE scope=? AND key=?",
                ("global", "fnos_knowledge_engine_0_13"),
            ).fetchone()
            marker = json.loads(row[0]) if row else None
            has_sources = bool(connection.execute("SELECT count(*) FROM sources").fetchone()[0])
            has_documents = bool(connection.execute("SELECT count(*) FROM documents").fetchone()[0])
            return classify_workspace(workspace, marker, has_sources=has_sources, has_documents=has_documents)
    except (OSError, sqlite3.Error, json.JSONDecodeError):
        raise ValueError(UNKNOWN_KNOWLEDGE) from None


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(classify_users(args.root))
    except (KeyError, OSError, ValueError):
        print(UNKNOWN_KNOWLEDGE, file=sys.stderr)
        sys.exit(1)
