"""Real SDK 0.15 SQLite/Lance retrieval through an HTTP JEV scorer.

Only embedding and extraction model responses are deterministic. The application
processor, SDK writers, recall, scope, event-to-chunk hydration and scorer client
all run unchanged; assertions inspect the scorer's actual inbound JSON.
"""

import asyncio
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiohttp import web
from zleap.sag.core.ai.embedding import EmbeddingClient
from zleap.sag.modules.extract.processor import EventProcessor

from sag_api.core.config import Settings
from sag_api.core.db import SessionLocal, init_db
from sag_api.core.errors import ApiError
from sag_api.db.models import Document, Job, Source
from sag_api.enums import DocumentStatus, JobStatus, JobType
from sag_api.sag.dto import ProcessCheckpoint
from sag_api.sag.engine_manager import EngineManager
from sag_api.sag.incremental_processor import IncrementalDocumentProcessor
from sag_api.services import retrieval_service


@pytest.fixture
async def native_jev(tmp_path, monkeypatch):
    async def embedding(_client, _text):
        return [1.0] + [0.0] * 15

    async def batch_embedding(client, texts):
        return [await embedding(client, text) for text in texts]

    async def extraction(processor, messages, _schema, **_kwargs):
        marker = re.search(r"WIRE_[A-Z_]+", str(messages[-1].content)).group()
        # Two real events reference one input chunk, with different final scores.
        return {"type": "response", "data": {"items": [
            {"reason": "The input states this private fact.", "title": f"{marker}_{level}",
             "summary": f"Private evidence {marker}.", "content": f"Private evidence {marker}.",
             "references": [1], "entities": [{"type": processor.entity_types[0].type,
                                                "name": marker, "description": "Private evidence"}]}
            for level in ("LOW", "HIGH")
        ]}}

    monkeypatch.setattr(EmbeddingClient, "generate", embedding)
    monkeypatch.setattr(EmbeddingClient, "batch_generate", batch_embedding)
    monkeypatch.setattr(EventProcessor, "_chat_with_schema_once", extraction)
    state = SimpleNamespace(requests=[], failure=None, failures_by_marker={}, block=False,
                            release=asyncio.Event(), started=asyncio.Event())

    async def score(request):
        payload = await request.json()
        state.requests.append({"path": request.path_qs, "authorization": request.headers.get("Authorization"),
                               "payload": payload, "transport": request.transport})
        state.started.set()
        failure = next((value for marker, value in state.failures_by_marker.items() if marker in payload["state"]),
                       state.failure)
        if failure == "timeout" or state.block:
            await state.release.wait()
        elif failure is not None:
            return web.json_response({"error": "private-provider-body"}, status=failure)
        candidates = dict(re.findall(r"^(\d+): ([^\n]+)", payload["state"], re.MULTILINE))
        assert set(candidates) == set(payload["questions"])
        return web.json_response({
            "model": payload["model"], "usage": {"input_tokens": 30, "output_tokens": 5},
            "answers": {key: (0.82 if title.endswith("_HIGH") else 0.23) for key, title in candidates.items()},
        })

    app = web.Application()
    app.router.add_post("/independent/noul", score)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    configuration = Settings(
        _env_file=None, data_dir=str(tmp_path / "engine"), sag_language="en",
        llm_api_key="llm-only-key", llm_model="fixture", embedding_model="fixture",
        embedding_api_key="embedding-only-key", embedding_schema_dimensions=16,
        jev_endpoint=f"http://127.0.0.1:{port}/independent/noul?contract=015",
        jev_model="independent-scorer-model", jev_api_key="jev-only-key",
        jev_ranking_timeout_seconds=2.0,
    )
    owner = EngineManager(configuration)
    await init_db()
    sources = {}

    async def add(config, marker, *, extracted=True, status=DocumentStatus.READY, control=None, deleted=False):
        if config not in sources:
            source = Source(name=f"wire-{config}", sag_source_config_id=f"{config}-{uuid4().hex}")
            async with SessionLocal() as session:
                session.add(source)
                await session.commit()
                await session.refresh(source)
            sources[config] = source
        source = sources[config]
        path = tmp_path / f"{marker}.md"
        path.write_text(f"# {marker}\n\nPrivate evidence {marker} is retained in this source.\n")
        checkpoint = ProcessCheckpoint()

        async def save(value):
            nonlocal checkpoint
            checkpoint = value.model_copy(deep=True)

        async def never_pause():
            return False

        async with owner.use(source.sag_source_config_id, source) as engine:
            processor = IncrementalDocumentProcessor(engine, source.sag_source_config_id, max_concurrency=1)
            if extracted:
                outcome = await processor.process(path, checkpoint=checkpoint,
                                                  on_checkpoint=save, should_pause=never_pause)
                assert outcome.event_count == 2
            else:
                await processor._ingest(checkpoint, path, save)
        assert len(checkpoint.chunk_ids) == 1
        document = Document(source_id=source.id, filename=path.name, content_type="text/markdown",
                            size_bytes=path.stat().st_size, storage_path=str(path), status=status,
                            sag_source_id=checkpoint.source_id)
        async with SessionLocal() as session:
            session.add(document)
            await session.flush()
            if control is not None:
                session.add(Job(source_id=source.id, document_id=document.id, type=JobType.REPROCESS_DOCUMENT,
                                status=JobStatus.QUEUED, payload={"derived_source_ids": [checkpoint.source_id],
                                                               "trigger": control}))
            if deleted:
                await session.delete(document)
            await session.commit()
        return SimpleNamespace(document_id=document.id, checkpoint=checkpoint, source=source, marker=marker)

    state.add = add
    state.owner = owner
    state.settings = configuration
    state.sources = sources
    try:
        yield state
    finally:
        state.release.set()
        await owner.aclose_all()
        await runner.cleanup()


async def assert_scorer_sockets_closed(state):
    for _ in range(20):
        if all(item["transport"].is_closing() for item in state.requests):
            return
        await asyncio.sleep(0.01)
    assert all(item["transport"].is_closing() for item in state.requests)


@pytest.mark.asyncio
async def test_native_scores_project_multiple_events_and_keep_unscored_chunks_last(native_jev):
    state = native_jev
    scored = await state.add("a", "WIRE_SCORED")
    unscored = await state.add("a", "WIRE_UNSCORED", extracted=False)
    other = await state.add("b", "WIRE_OTHER")
    single = await state.owner.search(scored.source.sag_source_config_id, "private evidence",
                                      source=scored.source, strategy="multi_jev", top_k=10)
    assert [section.chunk_id for section in single.sections] == [
        scored.checkpoint.chunk_ids[0], unscored.checkpoint.chunk_ids[0],
    ]
    await assert_scorer_sockets_closed(state)
    state.requests.clear()
    targets = [(source.sag_source_config_id, source) for source in state.sources.values()]
    result = await state.owner.search_many(targets, "private evidence", strategy="multi_jev", top_k=10)

    assert result.stats["requested_strategy"] == result.stats["effective_strategy"] == "multi_jev"
    assert result.stats["fallback_used"] is False
    assert result.stats["ranking"] == {
        "method": "jev", "fallback_reason": None, "scored_chunks": 2, "query_chunk_fallback_chunks": 1,
    }
    assert len(state.requests) == 2
    assert all(item["path"] == "/independent/noul?contract=015" for item in state.requests)
    assert all(item["authorization"] == "Bearer jev-only-key" for item in state.requests)
    assert all(item["payload"]["model"] == "independent-scorer-model" for item in state.requests)
    sections = {section.chunk_id: section for section in result.sections}
    for doc in (scored, other):
        section = sections[doc.checkpoint.chunk_ids[0]]
        assert section.score == pytest.approx(0.82)
        assert section.metadata["score_source"] == "jev"
        assert set(section.metadata["event_ids"]) == set(doc.checkpoint.event_ids)
        assert len(section.metadata["event_ids"]) == 2
    raw = sections[unscored.checkpoint.chunk_ids[0]]
    assert raw.metadata["recall_channel"] == raw.metadata["score_source"] == "query_chunk_fallback"
    assert not raw.metadata.get("event_ids")
    assert raw.score > 0.82  # A vector similarity must not outrank JEV evidence.
    assert result.sections[-1].chunk_id == raw.chunk_id
    assert all(section.metadata["score_source"] == "jev" for section in result.sections[:-1])
    for item in state.requests:
        text = item["payload"]["state"]
        assert ("WIRE_SCORED" in text) != ("WIRE_OTHER" in text)
        assert "WIRE_UNSCORED" not in text
    assert len(result.stats["source_stats"]) == 2
    await assert_scorer_sockets_closed(state)


@pytest.mark.asyncio
async def test_no_event_source_returns_native_chunk_fallback_without_scorer(native_jev):
    state = native_jev
    doc = await state.add("a", "WIRE_NO_EVENTS", extracted=False)
    result = await retrieval_service.retrieve_relevant_sections(
        state.owner, [doc.source], "private evidence", strategy="multi_jev",
    )
    assert [section.chunk_id for section in result.sections] == doc.checkpoint.chunk_ids
    assert result.sections[0].metadata["score_source"] == "query_chunk_fallback"
    assert result.sections[0].metadata["recall_channel"] == "query_chunk_fallback"
    assert not result.sections[0].metadata.get("event_ids")
    assert result.stats["effective_strategy"] == "multi_jev"
    assert result.stats["source_stats"][0]["ranking"] == {
        "method": "jev", "fallback_reason": None, "scored_chunks": 0, "query_chunk_fallback_chunks": 1,
    }
    assert state.requests == []


@pytest.mark.asyncio
async def test_mixed_source_success_error_fallback_and_no_event_scores_have_distinct_order(native_jev):
    state = native_jev
    scored = await state.add("a", "WIRE_GOOD")
    degraded = await state.add("b", "WIRE_DEGRADED")
    unscored = await state.add("c", "WIRE_NO_EVENTS", extracted=False)
    state.failures_by_marker = {"WIRE_DEGRADED": 503}
    state.settings.jev_failure_policy = "fallback_to_score"
    result = await state.owner.search_many(
        [(source.sag_source_config_id, source) for source in state.sources.values()],
        "private evidence", strategy="multi_jev", top_k=10,
    )
    assert [section.chunk_id for section in result.sections] == [
        scored.checkpoint.chunk_ids[0], degraded.checkpoint.chunk_ids[0], unscored.checkpoint.chunk_ids[0],
    ]
    assert [section.metadata["score_source"] for section in result.sections] == [
        "jev", "event_score", "query_chunk_fallback",
    ]
    assert result.sections[0].score < result.sections[1].score
    assert result.stats["fallback_used"] is True
    by_source = {item["source_config_id"]: item["ranking"] for item in result.stats["source_stats"]}
    assert by_source[scored.source.sag_source_config_id]["fallback_reason"] is None
    assert by_source[degraded.source.sag_source_config_id]["fallback_reason"]
    assert by_source[unscored.source.sag_source_config_id]["fallback_reason"] is None
    assert len(state.requests) == 2
    await assert_scorer_sockets_closed(state)


@pytest.mark.asyncio
async def test_persisted_visibility_is_enforced_before_real_scorer_and_business_reranking(native_jev, monkeypatch):
    state = native_jev
    visible = await state.add("a", "WIRE_VISIBLE")
    hidden = [
        await state.add("a", "WIRE_DELETED", deleted=True),
        await state.add("a", "WIRE_DELETING", status=DocumentStatus.DELETING),
        await state.add("a", "WIRE_DELETE_FAILED", status=DocumentStatus.DELETE_FAILED),
        await state.add("a", "WIRE_REPROCESSING", control="manual"),
        await state.add("a", "WIRE_NAS_CHANGED", control="nas_change"),
    ]
    await state.add("b", "WIRE_OTHER_TENANT")

    def forbidden(*_args, **_kwargs):
        pytest.fail("Business recall or reranking must not replace native JEV evidence")

    monkeypatch.setattr(retrieval_service, "_lexical_sections", forbidden)
    monkeypatch.setattr(retrieval_service, "rerank_sections", forbidden)
    result = await retrieval_service.retrieve_relevant_sections(
        state.owner, [visible.source], "unrelated words", strategy="multi_jev", top_k=10,
    )
    assert [section.chunk_id for section in result.sections] == visible.checkpoint.chunk_ids
    assert result.sections[0].score == pytest.approx(0.82)
    assert result.sections[0].metadata["score_source"] == "jev"
    assert len(state.requests) == 1
    outbound = state.requests[0]["payload"]["state"]
    assert "WIRE_VISIBLE" in outbound and "WIRE_OTHER_TENANT" not in outbound
    assert all(document.marker not in outbound for document in hidden)
    assert len(state.requests[0]["payload"]["questions"]) == 2

    # Removing the last visible input must not reinterpret () as an unbounded SDK scope.
    async with SessionLocal() as session:
        document = await session.get(Document, visible.document_id)
        document.status = DocumentStatus.DELETING
        await session.commit()
    empty = await retrieval_service.retrieve_relevant_sections(
        state.owner, [visible.source], "private evidence", strategy="multi_jev",
    )
    assert empty.sections == [] and len(state.requests) == 1
    await assert_scorer_sockets_closed(state)


@pytest.mark.asyncio
async def test_delete_committed_during_real_scoring_is_rechecked_before_return(native_jev):
    state = native_jev
    doc = await state.add("a", "WIRE_IN_FLIGHT")
    state.block = True
    task = asyncio.create_task(retrieval_service.retrieve_relevant_sections(
        state.owner, [doc.source], "private evidence", strategy="multi_jev", include_ranked_candidates=True,
    ))
    try:
        await asyncio.wait_for(state.started.wait(), 2)
        async with SessionLocal() as session:
            document = await session.get(Document, doc.document_id)
            document.status = DocumentStatus.DELETING
            await session.commit()
    finally:
        state.release.set()
    result = await task
    assert "WIRE_IN_FLIGHT" in state.requests[0]["payload"]["state"]
    assert result.sections == [] and result.stats["logically_deleted_filtered"] == 1
    assert "WIRE_IN_FLIGHT" not in result.model_dump_json()
    assert result.stats["ranked_candidates"] == []
    assert len(state.requests) == 1
    await assert_scorer_sockets_closed(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 503, "timeout"])
@pytest.mark.parametrize("policy", ["error", "fallback_to_score"])
async def test_real_scorer_failure_obeys_ranking_policy(native_jev, failure, policy):
    state = native_jev
    doc = await state.add("a", "WIRE_FAILURE")
    state.failure = failure
    state.settings.jev_failure_policy = policy
    state.settings.jev_ranking_timeout_seconds = 0.1 if failure == "timeout" else 2.0

    async def search():
        return await state.owner.search_many(
            [(doc.source.sag_source_config_id, doc.source)], "private evidence", strategy="multi_jev",
        )

    if policy == "error":
        with pytest.raises(ApiError) as raised:
            await search()
        assert str(raised.value.stage) == "retrieve"
        assert "private-provider-body" not in str(raised.value)
    else:
        result = await search()
        assert result.sections and result.stats["fallback_used"] is True
        assert result.stats["requested_strategy"] == result.stats["effective_strategy"] == "multi_jev"
        assert result.stats["ranking"]["method"] == "jev"
        assert result.stats["ranking"]["fallback_reason"]
        assert result.stats["source_stats"][0]["ranking"]["fallback_reason"]
        assert all(section.metadata["score_source"] == "event_score" for section in result.sections)
        assert "private-provider-body" not in str(result.stats)
    assert len(state.requests) == 1  # Production config disables scorer retries.
    await assert_scorer_sockets_closed(state)
