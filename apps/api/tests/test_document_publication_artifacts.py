"""Publication identity at exported packages and final agent evidence boundaries."""

from types import SimpleNamespace

import pytest

from sag_api.core.db import SessionLocal
from sag_api.db.models import Document
from sag_api.sag import RetrievedSection, SearchOutcome
from sag_api.services.agent_tooling import _adapt_tool
from sag_api.tools.base import ToolContext
from sag_api.tools.builtin import SearchContextTool
from tests.test_document_published_content import publication_body as publication_body
from tests.test_document_published_content import publication_evidence as publication_evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["stable", "provided_switch", "fallback_switch"])
async def test_final_agent_artifact_keeps_one_current_publication(publication_evidence, monkeypatch, stage):
    from sag_api.tools import builtin

    manager, source, document, _entity, published_chunk, *_ = publication_evidence

    async def retrieve(*args, **kwargs):
        return SearchOutcome(
            query="Acme",
            sections=[
                RetrievedSection(
                    source_config_id=source.sag_source_config_id,
                    source_id=document.sag_source_id,
                    chunk_id=published_chunk,
                    content="published-original-chunk",
                    score=0.8,
                )
            ],
        )

    async def recall(*args, **kwargs):
        return {}

    monkeypatch.setattr(builtin, "retrieve_relevant_sections", retrieve)
    monkeypatch.setattr(builtin, "recall_event_scores", recall)
    graph_calls = []
    original_graph = manager.graph_for_sections

    async def graph(*args, **kwargs):
        graph_calls.append(args)
        return await original_graph(*args, **kwargs)

    monkeypatch.setattr(manager, "graph_for_sections", graph)

    class BoundarySearch(SearchContextTool):
        async def invoke(self, args, context):
            result = await super().invoke(args, context)
            if stage == "fallback_switch":
                result.data.pop("_graph")
            if stage != "stable":
                async with SessionLocal() as session:
                    current = await session.get(Document, document.id)
                    current.sag_source_id = "candidate-input"
                    await session.commit()
            return result

    citations = []
    tool = _adapt_tool(BoundarySearch(), ToolContext(engine_manager=manager, sources=[source]), citations)
    result = await tool.execute(
        {"query": "Acme", "top_k": 1},
        SimpleNamespace(cancellation=SimpleNamespace(raise_if_cancelled=lambda: None)),
    )
    assert len(graph_calls) == 1
    assert "hidden-candidate" not in result.content
    if stage == "stable":
        assert "published-original-chunk" in result.content
        assert len(citations) == 1
        assert [
            node["label"] for node in result.artifacts["universe_activation"]["nodes"] if node["kind"] == "event"
        ] == ["Published event"]
    else:
        assert "published-original" not in result.content
        assert result.details["count"] == 0
        assert citations == result.artifacts["citations"] == []
        assert not result.artifacts.get("universe_activation", {}).get("nodes")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["source", "document"])
@pytest.mark.parametrize("state", ["paused", "switched", "deleting"])
async def test_actual_export_keeps_its_frozen_publication(publication_evidence, tmp_path, scope, state):
    from octx import open_octx, validate_octx
    from sqlalchemy import update
    from zleap.sag.db.models import Article, ArticleParseStatus, SourceChunk, SourceEvent

    from sag_api.core.config import Settings
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import OctxRelease, OctxTransfer
    from sag_api.enums import DocumentStatus, OctxTransferStatus
    from sag_api.octx.runner import OctxRunner
    from sag_api.octx.storage import OctxStorage
    from sag_api.services.octx_export import create_document_export_transfer, create_export_transfer, execute_export

    manager, source, document, *_ = publication_evidence
    engine_sessions = await manager.get_sag_session_factory(source.sag_source_config_id)
    async with engine_sessions() as session:
        session.add(
            Article(
                id="candidate-input",
                data_source_id=source.sag_source_config_id,
                title="Unpublished candidate",
                content="# Unpublished candidate\n",
                status="COMPLETED",
                parse_status=ArticleParseStatus.COMPLETED,
            )
        )
        await session.flush()
        for model in (SourceChunk, SourceEvent):
            await session.execute(
                update(model)
                .where(model.data_source_id == source.sag_source_config_id)
                .values(article_id=model.source_id)
            )
        await session.commit()

    class Queue:
        async def enqueue(self, _job_id):
            pass

    async with SessionLocal() as session:
        if scope == "source":
            transfer = await create_export_transfer(session, source.id, version=None, job_queue=Queue())
        else:
            transfer = await create_document_export_transfer(
                session, source.id, document.id, version=None, job_queue=Queue()
            )
        transfer_id = transfer.id
        frozen_ids = transfer.checkpoint["selected_article_ids"]

    if state != "paused":
        async with SessionLocal() as session:
            current = await session.get(Document, document.id)
            if state == "switched":
                current.sag_source_id = "candidate-input"
            else:
                current.status = DocumentStatus.DELETING
            await session.commit()

    storage = OctxStorage(tmp_path / "packages", max_upload_bytes=1024)
    async with SessionLocal() as session:
        transfer = await session.get(OctxTransfer, transfer_id)
        if state != "paused":
            with pytest.raises(ConflictError, match="frozen publication selection"):
                await execute_export(session, transfer, storage=storage, runner=None, engine_manager=manager)
            assert transfer.status is OctxTransferStatus.QUEUED
            assert transfer.release_id is None
            assert transfer.checkpoint["selected_article_ids"] == frozen_ids
            assert not storage.staging_dir(transfer_id).exists()
            return
        await execute_export(
            session,
            transfer,
            storage=storage,
            runner=OctxRunner(Settings(_env_file=None, octx_worker_timeout_seconds=30)),
            engine_manager=manager,
            sag_session_factory=engine_sessions,
        )
        assert transfer.status is OctxTransferStatus.READY
        release = await session.get(OctxRelease, transfer.release_id)
        artifact = storage.resolve_key(release.artifact_key)
        report = validate_octx(artifact)
        assert report.valid and report.fully_validated
        with open_octx(artifact) as package:
            exported = list(package.iter_documents())
            assert len(exported) == 1
            assert "Published original" in exported[0].body
            assert "Unpublished candidate" not in exported[0].body
            assert [chunk["text"] for chunk in package.iter_chunks()] == ["published-original-chunk"]
            assert [event["title"] for event in package.iter_events()] == ["Published event"]
