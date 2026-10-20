"""JEV owns final scoring and uses a scoped native engine request."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from sag_api.core.config import Settings
from sag_api.core.errors import ServiceUnavailableError
from sag_api.sag.dto import RetrievedSection, SearchOutcome
from sag_api.sag.engine_manager import EngineManager


def manager():
    return EngineManager(Settings(
        _env_file=None, jev_endpoint="https://scorer.invalid/score", jev_api_key="independent",
    ))


@pytest.mark.asyncio
async def test_native_jev_receives_input_allowlist_and_ranking_budget():
    owner = manager()
    requests = []

    class Engine:
        async def search(self, request):
            requests.append(request)
            return SimpleNamespace(query="q", sections=[], stats={"ranking": {"method": "jev"}})

    @asynccontextmanager
    async def use(*_args):
        yield Engine()

    owner.use = use
    await owner.search_many(
        [("config-a", None)], "q", strategy="multi_jev", source_ids_by_config={"config-a": ("visible",)},
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.scope.data_source_ids == ("config-a",)
    assert request.scope.source_ids == ("visible",)
    assert str(request.options.strategy) == "pruned_expand_jev"
    assert request.options.ranking.timeout_seconds == 30.0
    assert request.options.ranking.on_timeout == "error"
    await owner.search_many([("config-a", None)], "q", strategy="multi_jev", source_ids_by_config={"config-a": ()})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_jev_fanout_failure_waits_for_other_scorer_cleanup():
    owner = manager()
    entered = asyncio.Event()
    draining = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    async def search(scid, *_args, **_kwargs):
        if scid == "bad":
            await entered.wait()
            raise ServiceUnavailableError("synthetic scorer failure")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            closed.set()

    owner.search = search
    task = asyncio.create_task(owner.search_many([("bad", None), ("slow", None)], "q", strategy="multi_jev"))
    try:
        await asyncio.wait_for(draining.wait(), 1)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(ServiceUnavailableError):
        await task
    assert closed.is_set()


@pytest.mark.asyncio
async def test_jev_merge_does_not_mix_unscored_fallback_ahead_of_scored_evidence():
    owner = manager()

    async def search(scid, *_args, **_kwargs):
        return SearchOutcome(query="q", sections=[RetrievedSection(
            chunk_id=scid, content=scid, score=0.2 if scid == "scored" else 0.99,
            metadata={"score_source": "jev" if scid == "scored" else "query_chunk_fallback"},
        )], stats={"ranking": {"method": "jev", "fallback_reason": None}})

    owner.search = search
    outcome = await owner.search_many([("raw", None), ("scored", None)], "q", strategy="multi_jev")
    assert [item.chunk_id for item in outcome.sections] == ["scored", "raw"]
    assert outcome.stats["effective_strategy"] == "multi_jev"


@pytest.mark.asyncio
async def test_jev_business_path_scopes_before_scoring_and_rechecks_visibility(monkeypatch):
    from uuid import uuid4

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.retrieval_service import retrieve_relevant_sections

    await init_db()
    config_id = uuid4().hex
    async with SessionLocal() as session:
        source = Source(name="jev-visibility", sag_source_config_id=config_id)
        session.add(source)
        await session.flush()
        documents = []
        for article, status in [("visible", DocumentStatus.READY), ("paused", DocumentStatus.PAUSED),
                                ("deleting", DocumentStatus.DELETING), ("replacing", DocumentStatus.READY)]:
            document = Document(source_id=source.id, filename=f"{article}.md", content_type="text/markdown",
                                size_bytes=10, storage_path=f"/tmp/{article}.md", status=status, sag_source_id=article)
            session.add(document)
            documents.append(document)
        await session.flush()
        session.add(Job(source_id=source.id, document_id=documents[-1].id, type=JobType.REPROCESS_DOCUMENT,
                        status=JobStatus.QUEUED, payload={"derived_source_ids": ["replacing"]}))
        await session.commit()

        class NativeEngine:
            async def search_many(self, _targets, query, **options):
                assert options["source_ids_by_config"] == {config_id: ("paused", "visible")}
                # A deletion commits while the external scorer is in flight.
                documents[0].status = DocumentStatus.DELETING
                await session.commit()
                return SearchOutcome(query=query, sections=[
                    RetrievedSection(chunk_id="gone", source_id="visible", source_config_id=config_id,
                                     content="gone", score=.99),
                    RetrievedSection(chunk_id="kept", source_id="paused", source_config_id=config_id,
                                     content="unrelated native semantic evidence", score=.17,
                                     metadata={"score_source": "jev"}),
                ], stats={"ranking": {"method": "jev"}, "effective_strategy": "multi_jev"})

            async def grep_chunks(self, *_args, **_kwargs):
                pytest.fail("JEV must not invoke business lexical recall/reranking")

        result = await retrieve_relevant_sections(NativeEngine(), [source], "different query", strategy="multi_jev")
    assert [(section.chunk_id, section.score) for section in result.sections] == [("kept", .17)]
    assert result.stats["logically_deleted_filtered"] == 1


@pytest.mark.asyncio
async def test_jev_fallback_diagnostics_do_not_reflect_provider_output():
    owner = manager()

    class Engine:
        async def search(self, request):
            return SimpleNamespace(query="q", sections=[], stats={"ranking": {
                "method": "jev", "fallback_reason": "invalid_model_output: provider-secret /internal/path",
            }})

    @asynccontextmanager
    async def use(*_args):
        yield Engine()

    owner.use = use
    outcome = await owner.search("config", "q", strategy="multi_jev")
    assert outcome.stats["ranking"]["fallback_reason"] == "unavailable"
    assert "provider-secret" not in outcome.model_dump_json()
