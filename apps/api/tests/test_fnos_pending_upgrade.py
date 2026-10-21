"""Normal workers never mutate an unupgraded legacy knowledge library."""

import asyncio
import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest


def old_workspace(tmp_path: Path) -> Path:
    with tarfile.open(Path(__file__).parent / "fixtures/fnos_071/native-071.tar.gz") as archive:
        archive.extractall(tmp_path, filter="data")
    return tmp_path / "users/1000"


def hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*") if p.is_file()
    }


def test_read_only_tenant_classification_requires_real_data(tmp_path):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    assert classify_tenant(tmp_path / "absent") == "fresh"
    workspace = old_workspace(tmp_path)
    before = hashes(workspace)
    assert classify_tenant(workspace) == "legacy"
    assert hashes(workspace) == before
    (workspace / "meta/sag.db").write_bytes(b"broken")
    with pytest.raises(ValueError):
        classify_tenant(workspace)


def test_migration_marker_cannot_hide_missing_index_with_existing_documents(tmp_path):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    workspace = old_workspace(tmp_path)
    with sqlite3.connect(workspace / "meta/sag.db") as connection:
        connection.execute("INSERT INTO settings(scope,key,value_json,id) VALUES(?,?,?,?)",
                           ("global", "fnos_knowledge_engine_0_13",
                            '{"engine":"0.13.0","knowledge_reset":true}', "partial-restore"))
    before = hashes(workspace)
    with pytest.raises(ValueError):
        classify_tenant(workspace)
    assert hashes(workspace) == before


@pytest.mark.parametrize("database_kind", ["meta", "engine"])
@pytest.mark.parametrize("keep_shm", [False, True])
def test_classification_reads_cold_committed_wal_without_changing_source_sidecars(tmp_path, database_kind, keep_shm):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    workspace = old_workspace(tmp_path)
    database = workspace / database_kind / "sag.db"
    wal = database.with_name(database.name + "-wal")
    shm = database.with_name(database.name + "-shm")
    # A real writer checkpoints, commits WAL-only schema/marker changes, and
    # dies before SQLite can checkpoint or remove its source-side sidecars.
    writer = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import os
            import signal
            import sqlite3
            import sys

            connection = sqlite3.connect(sys.argv[1])
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            if sys.argv[2] == "engine":
                # The cold WAL is necessary to restore the recognized schema.
                connection.execute("ALTER TABLE source_config RENAME TO source_config_pending")
                connection.commit()
                assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
                connection.execute("ALTER TABLE source_config_pending RENAME TO source_config")
            else:
                # Keep the reset marker only in WAL after clearing old documents.
                for table in ("documents", "sources", "jobs"):
                    connection.execute(f'DELETE FROM "{table}"')
                connection.commit()
                assert connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
                connection.execute(
                    "INSERT INTO settings (scope, key, value_json, id) VALUES (?, ?, ?, ?)",
                    ("global", "fnos_knowledge_engine_0_13", '{"engine":"0.13.0","knowledge_reset":true}',
                     "wal-marker"),
                )
            connection.commit()
            os.kill(os.getpid(), signal.SIGKILL)
        """), str(database), database_kind],
        capture_output=True, text=True, timeout=30,
    )
    assert writer.returncode == -signal.SIGKILL, writer.stderr
    assert wal.stat().st_size > 0
    assert shm.is_file()
    if not keep_shm:
        shm.unlink()
    before_tree = sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*"))
    before_hashes = hashes(workspace)
    source_files = [path for path in (database, wal, shm) if path.exists()]
    before_sidecars = {
        path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in source_files
    }

    for _ in range(120):
        assert classify_tenant(workspace) == ("compatible" if database_kind == "meta" else "legacy")
        assert {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in source_files
        } == before_sidecars

    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")) == before_tree
    assert hashes(workspace) == before_hashes
    assert shm.exists() == keep_shm


@pytest.mark.parametrize("database_kind", ["meta", "engine"])
def test_classification_does_not_create_sidecars_for_checkpointed_wal_database(tmp_path, database_kind):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    workspace = old_workspace(tmp_path)
    database = workspace / database_kind / "sag.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    assert not database.with_name(database.name + "-wal").exists()
    assert not database.with_name(database.name + "-shm").exists()
    before_tree = sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*"))
    before_hashes = hashes(workspace)

    assert classify_tenant(workspace) == "legacy"

    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*")) == before_tree
    assert hashes(workspace) == before_hashes


def test_classifier_does_not_follow_tenant_symlink(tmp_path):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    workspace = old_workspace(tmp_path)
    link = tmp_path / "other"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(ValueError):
        classify_tenant(link)


def test_worker_check_rejects_a_linked_tenant_parent(tmp_path):
    from sag_api.fnos.supervisor import _workspace_state

    workspace = old_workspace(tmp_path)
    link = tmp_path / "linked-users"
    link.symlink_to(workspace.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        _workspace_state(link / workspace.name)


@pytest.mark.asyncio
async def test_tenant_check_does_not_block_other_gateway_requests(tmp_path, monkeypatch):
    from sag_api.fnos import supervisor as module
    from sag_api.fnos.identity import GatewayIdentity

    progressed = threading.Event()
    observed = []

    def slow_check(_path):
        observed.append(progressed.wait(timeout=1))
        return "legacy"

    monkeypatch.setattr(module, "classify_tenant", slow_check)
    supervisor = module.WorkerSupervisor(tmp_path, tmp_path / "run")

    async def other_request():
        await asyncio.sleep(0)
        progressed.set()

    heartbeat = asyncio.create_task(other_request())
    with pytest.raises(module.WorkerStartError):
        await supervisor.acquire(GatewayIdentity(1000, "alice", True))
    await heartbeat
    assert observed == [True]


def test_classifier_rejects_special_files_without_metadata(tmp_path):
    from sag_api.fnos.knowledge_compatibility import classify_tenant

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    os.mkfifo(uploads / "unrecognized.pipe")
    with pytest.raises(ValueError):
        classify_tenant(tmp_path)


@pytest.mark.asyncio
async def test_normal_fnos_start_does_not_initialize_or_reset_pending_knowledge(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from sag_api import main

    workspace = old_workspace(tmp_path)
    before = hashes(workspace)
    monkeypatch.setattr(main, "settings", SimpleNamespace(
        environment="dev", auth_mode="fnos", data_dir=str(workspace / "engine-v0.13-clean"),
        upload_dir=str(workspace / "uploads"), debug=False,
    ))

    async def forbidden_init():
        pytest.fail("metadata migration must not run before explicit confirmation")

    monkeypatch.setattr(main, "init_db", forbidden_init)
    with pytest.raises(ValueError, match="confirm"):
        async with main._running_lifespan(FastAPI()):
            pytest.fail("pending knowledge must not enter normal runtime")
    assert hashes(workspace) == before


@pytest.mark.asyncio
async def test_reset_defaults_to_refusing_legacy_knowledge(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    workspace = old_workspace(tmp_path)
    before = hashes(workspace)
    engine = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
    try:
        async with async_sessionmaker(engine)() as session:
            with pytest.raises(ValueError, match="confirm"):
                await reset_legacy_knowledge(session, workspace / "engine")
    finally:
        await engine.dispose()
    assert hashes(workspace) == before


@pytest.mark.asyncio
async def test_supervisor_refuses_pending_workspace_before_spawning_or_preparing(tmp_path):
    from sag_api.fnos.identity import GatewayIdentity
    from sag_api.fnos.supervisor import WorkerStartError, WorkerSupervisor

    workspace = old_workspace(tmp_path)
    before = hashes(workspace)

    class NoSpawn:
        async def spawn(self, *_args):
            pytest.fail("pending workspace must not spawn a worker")

    supervisor = WorkerSupervisor(
        tmp_path, tmp_path / "temp", process_factory=NoSpawn(),
    )
    try:
        for _ in range(2):
            with pytest.raises(WorkerStartError):
                await supervisor.acquire(GatewayIdentity(1000, "sag1", True))
            assert supervisor._starting == {}
        assert not (tmp_path / "temp").exists()
        assert not (workspace / "engine-v0.13-clean").exists()
        assert hashes(workspace) == before
    finally:
        await supervisor.close()
