"""Upgrade real 0.7.1 stores offline; model responses alone are deterministic."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FIXTURE = Path(__file__).parent / "fixtures" / "fnos_071"
ROOT = Path(__file__).resolve().parents[3]


def tree_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def unpack_071(pkgvar):
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    with tarfile.open(FIXTURE / "native-071.tar.gz") as archive:
        archive.extractall(pkgvar, filter="data")
    assert tree_hashes(pkgvar) == provenance["files"]
    for tenant in provenance["tenants"]:
        database = pkgvar / "users" / tenant / "meta" / "sag.db"
        with sqlite3.connect(database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
            assert "knowledge_state" not in columns
            # Relocate only the fixture's filesystem root, keeping legacy paths/layout.
            connection.execute(
                "UPDATE documents SET storage_path = replace(storage_path, ?, ?)",
                (provenance["path_prefix"], str(pkgvar)),
            )
        with sqlite3.connect(pkgvar / "users" / tenant / "engine" / "sag.db") as connection:
            assert connection.execute("SELECT count(*) FROM source_chunk").fetchone()[0] > 0
            assert connection.execute("SELECT count(*) FROM source_event").fetchone()[0] > 0
    return provenance["tenants"]


def test_real_071_two_tenant_cold_backup_restores_exact_data(tmp_path):
    pkgvar = tmp_path / "pkgvar"
    unpack_071(pkgvar)
    before = tree_hashes(pkgvar)
    command_dir = tmp_path / "cmd"
    command_dir.mkdir()
    appdest = tmp_path / "appdest"
    (appdest / "runtime").mkdir(parents=True)
    template = ROOT / "packages/fnos/native/sag"
    for name in ("upgrade_init", "upgrade_callback"):
        shutil.copy2(template / "cmd" / name, command_dir / name)
    shutil.copy2(template / "app/runtime/lifecycle.py", appdest / "runtime/lifecycle.py")
    trace = tmp_path / "trace"
    for name, body in {
        "main": 'printf "%s\\n" "$1" >> "$TRACE"; [ "$1" != status ]',
        "install_callback": "exit 0",
    }.items():
        script = command_dir / name
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
    log = tmp_path / "upgrade.log"
    env = {
        **os.environ,
        "TRIM_APPDEST": str(appdest),
        "TRIM_PKGVAR": str(pkgvar),
        "TRIM_TEMP_LOGFILE": str(log),
        "TRACE": str(trace),
    }

    declined = subprocess.run(
        ["/bin/sh", str(command_dir / "upgrade_callback")], env=env, capture_output=True, text=True, check=False
    )
    assert declined.returncode != 0 and not trace.exists()
    assert tree_hashes(pkgvar) == before

    env["SAG_ACCEPT_REINGEST_UPGRADE"] = "true"
    accepted = subprocess.run(
        ["/bin/sh", str(command_dir / "upgrade_callback")], env=env, capture_output=True, text=True, check=False
    )
    assert accepted.returncode == 0, accepted.stderr + log.read_text()
    backups = list((pkgvar / "backup").glob("*.tar.gz"))
    assert len(backups) == 1
    restored = tmp_path / "restored"
    with tarfile.open(backups[0]) as archive:
        archive.extractall(restored, filter="data")
    assert tree_hashes(restored) == before
    assert tree_hashes(pkgvar / "users") == tree_hashes(restored / "users")
    assert "start" not in trace.read_text().splitlines()

    # The new payload must never launch on failed backup validation. The prior
    # verified archive remains usable and restores the exact pre-upgrade tree.
    corrupted = pkgvar / "users/1000/meta/sag.db"
    corrupted.write_bytes(b"invalid SQLite")
    failed_before = tree_hashes(pkgvar / "users")
    failed = subprocess.run(
        ["/bin/sh", str(command_dir / "upgrade_callback")], env=env, capture_output=True, text=True, check=False
    )
    assert failed.returncode != 0
    assert "validate failed" in log.read_text()
    assert tree_hashes(pkgvar / "users") == failed_before
    assert "start" not in trace.read_text().splitlines()
    assert backups[0].is_file()
    with tarfile.open(backups[0]) as archive:
        archive.extractall(restored, filter="data")
    assert tree_hashes(restored) == before


@pytest.mark.asyncio
async def test_real_071_reset_is_tenant_scoped_and_preserves_recovery_files(tmp_path, monkeypatch):
    from sag_api.core import db
    from sag_api.db.base import Base
    from sag_api.db.models import Agent, Document, Message, Setting, Source, Thread
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    pkgvar = tmp_path / "pkgvar"
    tenants = unpack_071(pkgvar)
    untouched = tree_hashes(pkgvar / "users" / tenants[1])
    for tenant in tenants:
        workspace = pkgvar / "users" / tenant
        original_engine = tree_hashes(workspace / "engine")
        originals = tree_hashes(workspace / "uploads")
        engine = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db, "engine", engine)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await db._ensure_columns()
        async with sessions() as session:
            preserved = {}
            for model in (Agent, Thread, Message, Setting):
                preserved[model] = {row.id for row in (await session.scalars(select(model))).all()}
            assert await reset_legacy_knowledge(session, workspace / "engine") > 0
            assert (await session.scalars(select(Source))).all() == []
            assert (await session.scalars(select(Document))).all() == []
            for model in (Agent, Thread, Message, Setting):
                after = {row.id for row in (await session.scalars(select(model))).all()}
                assert preserved[model] <= after
            session.add(Source(name="New", sag_source_config_id="new"))
            await session.commit()
            assert await reset_legacy_knowledge(session, workspace / "engine") == 0
            assert len((await session.scalars(select(Source))).all()) == 1
        await engine.dispose()
        assert tree_hashes(workspace / "engine") == original_engine
        assert tree_hashes(workspace / "uploads") == originals
        if tenant == tenants[0]:
            assert tree_hashes(pkgvar / "users" / tenants[1]) == untouched
