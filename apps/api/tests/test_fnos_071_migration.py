"""Upgrade real 0.7.1 stores offline; model responses alone are deterministic."""

import asyncio
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
async def test_real_071_two_tenants_rebuild_only_after_consent_and_recover_queued_jobs(tmp_path, monkeypatch):
    from zleap.sag.core.adapters.defaults import OpenAIEmbeddingAdapter, OpenAILLMAdapter

    from sag_api.core import db
    from sag_api.core.config import Settings
    from sag_api.db.models import Agent, Document, Job, Message, Setting, Source, Thread
    from sag_api.enums import DocumentStatus, JobStatus
    from sag_api.fnos.knowledge_upgrade import (
        mark_legacy_knowledge_pending,
        queue_legacy_reingest,
        reingest_status,
        replace_missing_original,
    )
    from sag_api.jobs import InProcessAsyncQueue, inproc, tasks
    from sag_api.sag import EngineManager
    from sag_api.services import retrieval_service, universe_service

    tenants = unpack_071(tmp_path / "pkgvar")
    calls = []

    async def embedding(self, text):
        calls.append("embedding")
        return [1.0] + [0.0] * 7

    async def batch_embedding(self, texts):
        return [await embedding(self, text) for text in texts]

    async def llm(self, messages, response_schema, **kwargs):
        calls.append("llm")
        return {
            "type": "response",
            "data": {
                "items": [
                    {
                        "reason": "The supplied private original contains this knowledge.",
                        "title": "Retained knowledge",
                        "summary": "The private original survived upgrade.",
                        "content": "The private original survived upgrade.",
                        "references": [1],
                        "entities": [{"type": "concept", "name": "Migration", "description": "Private knowledge"}],
                        "is_valid": True,
                        "children": [],
                    }
                ]
            },
        }

    async def no_universe_refresh(*args, **kwargs):
        # Universe overview rendering is covered separately; this case runs real
        # document worker, parser, indexing, extraction, and retrieval paths.
        return None

    monkeypatch.setattr(OpenAIEmbeddingAdapter, "generate", embedding)
    monkeypatch.setattr(OpenAIEmbeddingAdapter, "batch_generate", batch_embedding)
    monkeypatch.setattr(OpenAILLMAdapter, "chat_with_schema", llm)
    monkeypatch.setattr(OpenAILLMAdapter, "chat_with_schema_once", llm)
    monkeypatch.setattr(universe_service, "schedule_universe_refresh", no_universe_refresh)

    class CommittedBeforeCrash:
        async def enqueue_durably(self, job_id):
            pass  # Simulate process exit after commit but before in-memory dispatch.

    other_tenant_before = tree_hashes(tmp_path / "pkgvar/users" / tenants[1])
    for index, tenant in enumerate(tenants):
        workspace = tmp_path / "pkgvar/users" / tenant
        legacy_before = tree_hashes(workspace / "engine")
        settings = Settings(
            _env_file=None,
            auth_mode="fnos",
            sag_language="en",
            database_url=f"sqlite+aiosqlite:///{workspace / 'meta/sag.db'}",
            data_dir=str(workspace / "engine-v0.13"),
            upload_dir=str(workspace / "uploads"),
            llm_api_key="fixture-only",
            embedding_api_key="fixture-only",
            embedding_schema_dimensions=8,
            embedding_request_dimensions=8,
            llm_model="fixture",
            embedding_model="fixture",
            engine_warmup_count=0,
        )
        database = create_async_engine(settings.database_url)
        sessions = async_sessionmaker(database, expire_on_commit=False)
        monkeypatch.setattr(db, "engine", database)
        monkeypatch.setattr(db, "SessionLocal", sessions)
        monkeypatch.setattr(db, "settings", settings)
        for module in (tasks, inproc, retrieval_service):
            monkeypatch.setattr(module, "settings", settings)
        monkeypatch.setattr(tasks, "SessionLocal", sessions)
        await db.init_db()  # Upgrade the real old metadata schema, not create a fresh database.
        manager = EngineManager(settings)
        queue = InProcessAsyncQueue(sessions, manager, concurrency=1)
        try:
            async with sessions() as session:
                assert await mark_legacy_knowledge_pending(session, workspace / "engine", workspace / "uploads") == 3
                assert await mark_legacy_knowledge_pending(session, workspace / "engine", workspace / "uploads") == 0
                assert (await session.get(Job, f"job-{tenant}")).status == JobStatus.PAUSED
                assert (await session.get(Agent, f"agent-{tenant}")).name == "Retained agent"
                assert (await session.get(Thread, f"thread-{tenant}")).title == "Retained conversation"
                saved = await session.scalar(select(Setting).where(Setting.key == "fixture_config"))
                assert saved.value["tenant"] == tenant
                citations = (await session.get(Message, f"message-{tenant}")).citations
                assert citations[0]["stale"] is True and "stale" not in citations[1]
                with pytest.raises(ValueError, match="not awaiting"):
                    await replace_missing_original(
                        session,
                        f"missing-{tenants[1 - index]}",
                        filename="missing.md",
                        content_type="text/markdown",
                        data=b"private",
                        uploads_dir=workspace / "uploads",
                    )
                await session.rollback()

            before_calls = len(calls)
            await queue.start()
            await asyncio.sleep(0.05)
            assert len(calls) == before_calls  # Old queued jobs must never call a model.
            await queue.stop()
            async with sessions() as session:
                status = await reingest_status(session)
                assert status["states"]["pending"] == 2 and status["states"]["needs_file"] == 1
                result = await queue_legacy_reingest(session, workspace / "uploads", CommittedBeforeCrash())
                assert result == {"queued": 2, "needs_file": 1}
                repeated = await queue_legacy_reingest(session, workspace / "uploads", CommittedBeforeCrash())
                assert repeated["queued"] == 0

            # A fresh queue instance must discover and run committed jobs after restart.
            queue = InProcessAsyncQueue(sessions, manager, concurrency=1)
            await queue.start()
            for _ in range(200):
                async with sessions() as session:
                    document = await session.get(Document, f"doc-{tenant}")
                    sheet = await session.get(Document, f"sheet-{tenant}")
                    if all(d.status in {DocumentStatus.READY, DocumentStatus.FAILED} for d in (document, sheet)):
                        break
                await asyncio.sleep(0.05)
            assert document.status == DocumentStatus.READY, document.error
            assert document.knowledge_state == "ready"
            assert sheet.status == DocumentStatus.READY, sheet.error
            assert sheet.knowledge_state == "ready"
            with sqlite3.connect(workspace / "engine-v0.13/sag.db") as connection:
                chunks = connection.execute(
                    "SELECT content, extra_data FROM source_chunk WHERE source_id = ?",
                    (sheet.sag_source_id,),
                ).fetchall()
            assert "R001" in " ".join(row[0] for row in chunks)
            assert "R002" in " ".join(row[0] for row in chunks)
            assert "record_group_id" in json.dumps(chunks)
            assert all(not ("R001" in row[0] and "R002" in row[0]) for row in chunks)
            await queue.stop()
            async with sessions() as session:
                source = await session.get(Source, f"source-{tenant}")
                outcome = await retrieval_service.retrieve_relevant_sections(
                    manager, [source], "private original", top_k=5
                )
                assert outcome.sections
                assert outcome.stats["knowledge_pending"] == 1
                await replace_missing_original(
                    session,
                    f"missing-{tenant}",
                    filename="missing.md",
                    content_type="text/markdown",
                    data=b"# Replacement\n\nPrivate restored knowledge.",
                    uploads_dir=workspace / "uploads",
                )
                assert (await reingest_status(session))["states"]["pending"] == 1
                assert (await queue_legacy_reingest(session, workspace / "uploads", CommittedBeforeCrash()))[
                    "queued"
                ] == 1
            queue = InProcessAsyncQueue(sessions, manager, concurrency=1)
            await queue.start()
            for _ in range(200):
                async with sessions() as session:
                    replacement = await session.get(Document, f"missing-{tenant}")
                    if replacement.status in {DocumentStatus.READY, DocumentStatus.FAILED}:
                        break
                await asyncio.sleep(0.05)
            assert replacement.status == DocumentStatus.READY, replacement.error
            await queue.stop()
            async with sessions() as session:
                assert (await reingest_status(session))["states"]["ready"] == 3
                assert (await queue_legacy_reingest(session, workspace / "uploads", CommittedBeforeCrash()))[
                    "queued"
                ] == 0
            assert tree_hashes(workspace / "engine") == legacy_before
            if index == 0:
                assert tree_hashes(tmp_path / "pkgvar/users" / tenants[1]) == other_tenant_before
        finally:
            await queue.stop()
            await manager.aclose_all()
            await database.dispose()
