"""Recover an interrupted first engine bootstrap without clearing knowledge."""

import pytest

from sag_api.fnos.knowledge_compatibility import UNKNOWN_KNOWLEDGE, classify_workspace

COMMITTED = {"engine": "0.13.0", "knowledge_reset": True}


@pytest.mark.parametrize("marker,has_sources,expected", [
    (None, False, "fresh"),
    (COMMITTED, False, "compatible"),
    (COMMITTED, True, "compatible"),
])
def test_empty_lancedb_bootstrap_can_resume_without_deleting_directories(tmp_path, marker, has_sources, expected):
    directory = tmp_path / "engine-v0.13-clean" / "lancedb"
    directory.mkdir(parents=True)

    for _ in range(2):
        assert classify_workspace(tmp_path, marker, has_sources=has_sources) == expected
        assert directory.is_dir()
        assert list(directory.iterdir()) == []


@pytest.mark.parametrize("marker", [None, COMMITTED])
def test_registered_documents_prevent_empty_bootstrap_recovery(tmp_path, marker):
    (tmp_path / "engine-v0.13-clean" / "lancedb").mkdir(parents=True)
    with pytest.raises(ValueError, match=UNKNOWN_KNOWLEDGE):
        classify_workspace(tmp_path, marker, has_sources=True, has_documents=True)


@pytest.mark.parametrize("defect", ["vector-file", "vector-subdirectory", "unknown-file", "unknown-directory",
                                  "database-sidecar", "corrupt-database", "vector-symlink", "vector-file-path"])
def test_unknown_or_populated_bootstrap_is_preserved_and_rejected(tmp_path, defect):
    current = tmp_path / "engine-v0.13-clean"
    vector = current / "lancedb"
    vector.mkdir(parents=True)
    if defect == "vector-file":
        (vector / "data.lance").write_bytes(b"existing knowledge")
    elif defect == "vector-subdirectory":
        (vector / "data.lance").mkdir()
    elif defect == "unknown-file":
        (current / "unknown").write_bytes(b"keep")
    elif defect == "unknown-directory":
        (current / "unknown").mkdir()
    elif defect == "database-sidecar":
        (current / "sag.db-wal").write_bytes(b"uncheckpointed data")
    elif defect == "corrupt-database":
        (current / "sag.db").write_bytes(b"damaged database")
    elif defect == "vector-symlink":
        vector.rmdir()
        target = tmp_path / "outside"
        target.mkdir()
        vector.symlink_to(target, target_is_directory=True)
    elif defect == "vector-file-path":
        vector.rmdir()
        vector.write_bytes(b"keep")
    before = {str(p.relative_to(current)): p.read_bytes() for p in current.rglob("*") if p.is_file()}

    with pytest.raises(ValueError, match=UNKNOWN_KNOWLEDGE):
        classify_workspace(tmp_path, COMMITTED, has_sources=True)

    assert {str(p.relative_to(current)): p.read_bytes() for p in current.rglob("*") if p.is_file()} == before
    assert vector.exists()


@pytest.mark.asyncio
async def test_real_sdk_interruption_before_database_creation_is_recoverable(tmp_path, monkeypatch):
    from zleap.sag import DataEngine
    from zleap.sag.core import resources

    from sag_api.core.config import settings
    from sag_api.sag.config_builder import build_engine_config

    current = tmp_path / "engine-v0.13-clean"
    config = build_engine_config(settings.model_copy(update={"data_dir": str(current)}))
    engine = DataEngine(config, data_source_id="bootstrap-regression", health_check=False)

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("interrupted bootstrap")

    with monkeypatch.context() as patch:
        patch.setattr(resources, "build_container", interrupted)
        with pytest.raises(Exception, match="interrupted bootstrap"):
            await engine.init_schema()

    assert (current / "lancedb").is_dir()
    assert not (current / "sag.db").exists()
    assert classify_workspace(tmp_path, COMMITTED, has_sources=True) == "compatible"

    await engine.init_schema()
    assert (current / "sag.db").is_file()
    assert classify_workspace(tmp_path, COMMITTED, has_sources=True) == "compatible"
    await engine.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["QUEUED", "RUNNING"])
@pytest.mark.parametrize("defect", [None, "ready-document", "source-chunks", "document-chunks", "checkpoint",
                                  "no-job", "finished-job", "wrong-source-job", "truncated-upload", "missing-upload",
                                  "outside-upload", "symlink-upload", "symlink-parent", "nonempty-vector",
                                  "blocked-job"])
async def test_first_upload_recovery_uses_durable_job_and_intact_original(tmp_path, job_status, defect):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sag_api.db.base import Base
    from sag_api.db.models import Document, Job, Setting, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.fnos.knowledge_compatibility import classify_tenant
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    (tmp_path / "meta").mkdir()
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta/sag.db'}")
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(db, expire_on_commit=False)
    vector = tmp_path / "engine-v0.13-clean/lancedb"
    vector.mkdir(parents=True)
    if defect == "nonempty-vector":
        (vector / "existing").write_text("keep")
    original = tmp_path / "uploads/source/doc.txt"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    storage_path = original
    if defect == "truncated-upload":
        original.write_bytes(b"short")
    elif defect == "missing-upload":
        original.unlink()
    elif defect == "outside-upload":
        storage_path = tmp_path / "outside.txt"
        storage_path.write_bytes(b"original")
    elif defect in {"symlink-upload", "symlink-parent"}:
        target = tmp_path / "outside"
        target.mkdir()
        (target / "doc.txt").write_bytes(b"original")
        original.unlink()
        if defect == "symlink-upload":
            original.symlink_to(target / "doc.txt")
        else:
            original.parent.rmdir()
            original.parent.symlink_to(target, target_is_directory=True)
    try:
        async with sessions() as session:
            source = Source(id="source", name="First", sag_source_config_id="first",
                            chunk_count=1 if defect == "source-chunks" else 0)
            session.add(source)
            await session.flush()
            document = Document(id="doc", source_id=source.id, filename="doc.txt", storage_path=str(storage_path),
                                size_bytes=8, status=DocumentStatus.READY if defect == "ready-document"
                                else (DocumentStatus.LOADING if job_status == "RUNNING" else DocumentStatus.PENDING),
                                chunk_count=1 if defect == "document-chunks" else 0)
            session.add_all([document, Setting(scope="global", key="fnos_knowledge_engine_0_13", value=COMMITTED),
                             Setting(scope="global", key="model_config", value={"llm_api_key": "preserved-test-key"})])
            await session.flush()
            if defect != "no-job":
                session.add(Job(type=JobType.PROCESS_DOCUMENT,
                                status=JobStatus.SUCCEEDED if defect == "finished-job" else JobStatus[job_status],
                                source_id=None if defect == "wrong-source-job" else source.id, document_id=document.id,
                                payload={
                                    "checkpoint": {"process_checkpoint": {"chunk_ids": ["already-indexed"]}},
                                    "blocked-job": {"_scheduler": {"blocked_reason": "source_maintenance"}},
                                }.get(defect, {})))
            await session.commit()
            if defect is None:
                assert classify_tenant(tmp_path) == "compatible"
                assert await reset_legacy_knowledge(session, tmp_path / "engine") == 0
                assert await session.get(Document, document.id) is not None
                assert (await session.scalar(select(Setting).where(Setting.key == "model_config"))).value == {
                    "llm_api_key": "preserved-test-key"
                }
                assert original.read_bytes() == b"original"
                from sag_api.jobs.inproc import InProcessAsyncQueue

                queue = InProcessAsyncQueue(sessions, None)
                await queue._recover()
                assert queue._queue.qsize() == 1
                async with sessions() as recovered:
                    assert (await recovered.scalar(select(Job))).status == JobStatus.QUEUED
            else:
                with pytest.raises(ValueError, match=UNKNOWN_KNOWLEDGE):
                    classify_tenant(tmp_path)
                with pytest.raises(ValueError, match=UNKNOWN_KNOWLEDGE):
                    await reset_legacy_knowledge(session, tmp_path / "engine")
    finally:
        await db.dispose()
