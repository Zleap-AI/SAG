"""Reset legacy knowledge once while retaining recovery files and user configuration."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_marker", [False, True])
async def test_reset_preserves_files_settings_chats_and_is_idempotent(tmp_path, previous_marker):
    from sag_api.db.base import Base
    from sag_api.db.models import Agent, AgentBinding, Document, Job, Message, Setting, Source, Thread
    from sag_api.enums import BindingTargetType, JobStatus, JobType, MessageRole
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'meta.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    legacy = tmp_path / "engine"
    legacy.mkdir()
    (legacy / "index").write_bytes(b"old engine")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "book.md").write_text("original")
    async with sessions() as session:
        source = Source(name="Old", sag_source_config_id="old")
        agent = Agent(name="Saved")
        session.add_all([source, agent])
        await session.flush()
        document = Document(source_id=source.id, filename="book.md", storage_path=str(uploads / "book.md"))
        thread = Thread(agent_id=agent.id, title="Saved chat")
        session.add_all([document, thread])
        await session.flush()
        session.add_all([
            Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED, source_id=source.id, document_id=document.id),
            AgentBinding(agent_id=agent.id, target_type=BindingTargetType.SOURCE, target_id=source.id),
            Setting(scope="global", key="model_config", value={"model": "saved"}),
            Message(thread_id=thread.id, role=MessageRole.ASSISTANT, content="Saved answer",
                    citations=[{"chunk_id": "old"}, {"kind": "external", "url": "https://example.com"}]),
        ])
        if previous_marker:
            session.add(Setting(scope="global", key="fnos_knowledge_engine_0_13", value={"engine": "0.13.0"}))
        await session.commit()
        assert await reset_legacy_knowledge(session, legacy) == 1
        for model in [Source, Document, Job, AgentBinding]:
            assert (await session.scalars(select(model))).all() == []
        message = await session.scalar(select(Message))
        assert message.content == "Saved answer"
        assert message.citations[0]["stale"] is True
        assert "stale" not in message.citations[1]
        assert await session.get(Thread, thread.id) is not None
        assert await session.get(Agent, agent.id) is not None
        assert (await session.scalar(select(Setting).where(Setting.key == "model_config"))).value == {"model": "saved"}
        fresh = Source(name="New", sag_source_config_id="new")
        session.add(fresh)
        await session.commit()
        assert await reset_legacy_knowledge(session, legacy) == 0
        assert await session.get(Source, fresh.id) is not None
    assert (legacy / "index").read_bytes() == b"old engine"
    assert (uploads / "book.md").read_text() == "original"
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_workspace_is_not_reset(tmp_path):
    from sag_api.db.base import Base
    from sag_api.db.models import Setting, Source
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        assert await reset_legacy_knowledge(session, tmp_path / "engine") == 0
        source = Source(name="Fresh", sag_source_config_id="fresh")
        session.add(source)
        await session.commit()
        assert await reset_legacy_knowledge(session, tmp_path / "engine") == 0
        assert await session.get(Source, source.id) is not None
        marker = await session.scalar(select(Setting).where(Setting.key == "fnos_knowledge_engine_0_13"))
        assert marker.value["knowledge_reset"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_removed_reingest_endpoint_cannot_queue_work():
    import httpx

    from sag_api.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/fnos/knowledge-upgrade/reingest")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_failed_reset_commit_does_not_lose_knowledge_or_write_marker(tmp_path, monkeypatch):
    from sag_api.db.base import Base
    from sag_api.db.models import Setting, Source
    from sag_api.fnos.knowledge_upgrade import reset_legacy_knowledge

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'atomic.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Source(name="Old", sag_source_config_id="old"))
        await session.commit()
        async def fail_commit():
            raise RuntimeError("commit interrupted")
        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            await reset_legacy_knowledge(session, tmp_path / "missing-engine")
        await session.rollback()
    async with sessions() as session:
        assert len((await session.scalars(select(Source))).all()) == 1
        assert await session.scalar(select(Setting).where(Setting.key == "fnos_knowledge_engine_0_13")) is None
    await engine.dispose()
