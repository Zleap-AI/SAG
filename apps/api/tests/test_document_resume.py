"""文档并发抽取的断点、暂停与继续行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sag_api.enums import DocumentStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden_status",
    [DocumentStatus.DELETING, DocumentStatus.DELETE_FAILED],
)
async def test_public_document_reads_hide_logically_deleted_rows(hidden_status):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import NotFoundError
    from sag_api.db.models import Document, Source
    from sag_api.services.document_service import get_document, get_public_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"hidden-read-{uuid4().hex}",
            sag_source_config_id=f"hidden-read-config-{uuid4().hex}",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="hidden.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/hidden.md",
            status=hidden_status,
        )
        session.add(document)
        await session.commit()

        assert await get_document(session, source, document.id) is not None
        with pytest.raises(NotFoundError, match="文档不存在"):
            await get_public_document(session, source, document.id)


@pytest.mark.asyncio
async def test_incremental_processor_pauses_after_inflight_chunks_and_resumes(monkeypatch):
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=2)
    active = 0
    peak_active = 0
    both_started = asyncio.Event()

    async def extract_chunk(chunk_id: str):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        if active == 2:
            both_started.set()
        await both_started.wait()
        await asyncio.sleep(0)
        active -= 1
        return [f"event-{chunk_id}"], 100

    normalized: list[list[str]] = []
    restored: list[list[str]] = []

    async def normalize(chunk_ids: list[str]):
        normalized.append(chunk_ids)

    async def restore(event_ids: list[str]):
        restored.append(list(event_ids))

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_normalize_event_ranks", normalize)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", restore)

    snapshots: list[ProcessCheckpoint] = []
    pause_requested = False

    async def on_checkpoint(value: ProcessCheckpoint):
        nonlocal pause_requested
        snapshots.append(value)
        pause_requested = True

    async def should_pause():
        return pause_requested

    initial = ProcessCheckpoint(chunk_ids=["c1", "c2", "c3", "c4", "c5"])
    paused = await processor.process(
        None,
        checkpoint=initial,
        on_checkpoint=on_checkpoint,
        should_pause=should_pause,
    )

    assert peak_active == 2
    assert paused.paused is True
    assert len(paused.processed_chunk_ids) == 2
    assert paused.token_usage == 200
    assert normalized == []
    assert restored
    assert set(restored[-1]) == {"event-c1", "event-c2"}

    pause_requested = False
    resumed = await processor.process(
        None,
        checkpoint=snapshots[-1],
        on_checkpoint=lambda value: _append_checkpoint(snapshots, value),
        should_pause=should_pause,
    )

    assert resumed.paused is False
    assert set(resumed.processed_chunk_ids) == {"c1", "c2", "c3", "c4", "c5"}
    assert resumed.event_count == 5
    assert resumed.token_usage == 500
    assert normalized == [["c1", "c2", "c3", "c4", "c5"]]
    assert set(restored[-1]) == {
        "event-c1",
        "event-c2",
        "event-c3",
        "event-c4",
        "event-c5",
    }


@pytest.mark.asyncio
async def test_incremental_processor_restores_events_before_publishing_checkpoint(monkeypatch):
    """The graph must be able to read every event advertised by a live checkpoint."""
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=1)
    restored: set[str] = set()
    snapshots: list[ProcessCheckpoint] = []

    async def extract_chunk(chunk_id: str):
        return {
            "chunk-1": ["event-1", "event-2"],
            "chunk-2": ["event-3"],
        }[chunk_id], 10

    async def restore(event_ids: list[str]):
        restored.update(event_ids)

    async def publish(value: ProcessCheckpoint):
        # `on_checkpoint` persists document.event_count. Once it becomes visible to
        # the detail page, those same event ids must already be visible to /graph.
        assert set(value.event_ids) <= restored
        snapshots.append(value)

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", restore)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(chunk_ids=["chunk-1", "chunk-2"]),
        on_checkpoint=publish,
        should_pause=_return_false,
    )

    assert [snapshot.event_count for snapshot in snapshots] == [2, 3]
    assert outcome.event_count == 3
    assert restored == {"event-1", "event-2", "event-3"}


@pytest.mark.asyncio
async def test_incremental_processor_passes_chunk_settings_to_zleap(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    seen = {}

    class FakeLoader:
        def __init__(self, *, parser=None):
            seen["fallback_title"] = parser.extract_title("普通正文，没有 Markdown 标题")
            seen["explicit_title"] = parser.extract_title("# 正文标题\n\n内容")

        async def load(self, config):
            seen["max_tokens"] = config.max_tokens
            seen["chunk_mode"] = config.chunk_mode
            return SimpleNamespace(source_id="document-1", chunk_ids=["chunk-1"])

    async def complete_headings(chunk_ids, source_config_id):
        seen["heading_vector_chunks"] = chunk_ids
        seen["heading_vector_source"] = source_config_id
        return 1

    async def pause_after_loading():
        return True

    monkeypatch.setattr(processor_module, "DocumentLoader", FakeLoader)
    monkeypatch.setattr(
        processor_module,
        "complete_loaded_chunk_heading_vectors",
        complete_headings,
    )
    processor = IncrementalDocumentProcessor(
        object(),
        "source-config",
        max_concurrency=2,
        chunk_max_tokens=1_600,
        chunk_mode="heading_strict",
        document_title="人类简史",
    )

    outcome = await processor.process(
        "/tmp/book.md",
        checkpoint=ProcessCheckpoint(),
        on_checkpoint=lambda value: _append_checkpoint([], value),
        should_pause=pause_after_loading,
    )

    assert seen == {
        "fallback_title": "人类简史",
        "explicit_title": "正文标题",
        "max_tokens": 1_600,
        "chunk_mode": "heading_strict",
        "heading_vector_chunks": ["chunk-1"],
        "heading_vector_source": "source-config",
    }
    assert outcome.source_id == "document-1"


@pytest.mark.asyncio
async def test_incremental_processor_records_successful_eventless_chunks(monkeypatch):
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=1)

    async def extract_chunk(_chunk_id: str):
        return [], 42

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", no_op)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)
    snapshots: list[ProcessCheckpoint] = []

    outcome = await processor.process(
        None,
        checkpoint=ProcessCheckpoint(chunk_ids=["chunk-without-event"]),
        on_checkpoint=lambda value: _append_checkpoint(snapshots, value),
        should_pause=_return_false,
    )

    assert outcome.processed_chunk_ids == ["chunk-without-event"]
    assert outcome.eventless_chunk_ids == ["chunk-without-event"]
    assert outcome.event_count == 0
    assert snapshots[-1].eventless_chunk_ids == ["chunk-without-event"]


@pytest.mark.asyncio
async def test_incremental_processor_unwraps_taskgroup_chunk_failure(monkeypatch):
    from zleap.sag.exceptions import ExtractError

    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(object(), "source-config", max_concurrency=2)

    async def extract_chunk(chunk_id: str):
        if chunk_id == "broken":
            raise ExtractError("结构化输出达到上限并被截断")
        await asyncio.sleep(0.01)
        return ["event-ok"], 10

    async def no_op(_ids):
        return None

    monkeypatch.setattr(processor, "_extract_chunk", extract_chunk)
    monkeypatch.setattr(processor, "_restore_checkpoint_events", no_op)
    monkeypatch.setattr(processor, "_normalize_event_ranks", no_op)

    with pytest.raises(ExtractError, match="达到上限并被截断"):
        await processor.process(
            None,
            checkpoint=ProcessCheckpoint(chunk_ids=["broken", "other"]),
            on_checkpoint=lambda value: _append_checkpoint([], value),
            should_pause=_return_false,
        )


@pytest.mark.asyncio
async def test_extract_chunk_tracks_tokens_from_wrapped_llm_client(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeLeafClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                content='{"data": {"items": []}}',
                usage=SimpleNamespace(total_tokens=321),
            )

    class FakeRetryClient:
        def __init__(self):
            self.client = FakeLeafClient()

        async def chat(self, messages, **kwargs):
            return await self.client.chat(messages, **kwargs)

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeRetryClient()

        async def _get_llm_client(self):
            return self.client

        async def extract(self, config):
            assert "观点、事实、定义" in config.custom_requirements
            assert config.enable_strict_filtering is False
            # zleap-sag 的重试客户端会让结构化输出直接调用内层客户端。
            await self.client.client.chat([SimpleNamespace(content="西游记")])
            return [SimpleNamespace(id="event-1")]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 321


@pytest.mark.asyncio
async def test_extract_chunk_normalizes_unambiguous_entity_type_alias(monkeypatch):
    import json

    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    payload = {
        "type": "response",
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "location": "中东",
                            "name": "中东",
                            "description": "尼安德特人演化的主要地区之一",
                        }
                    ]
                }
            ],
            "meta": {"reason": "ok"},
        },
    }

    class FakeLeafClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False),
                usage=SimpleNamespace(total_tokens=42),
            )

    class FakeRetryClient:
        def __init__(self):
            self.client = FakeLeafClient()

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeRetryClient()

        async def _get_llm_client(self):
            return self.client

        async def extract(self, config):
            request = {"data": {"meta": {"entity_types": [{"type": "location", "description": "地点"}]}}}
            response = await self.client.client.chat([SimpleNamespace(content=json.dumps(request, ensure_ascii=False))])
            entity = json.loads(response.content)["data"]["items"][0]["entities"][0]
            assert entity == {
                "name": "中东",
                "description": "尼安德特人演化的主要地区之一",
                "type": "location",
            }
            return [SimpleNamespace(id="event-1")]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 42


def test_extraction_response_does_not_guess_ambiguous_entity_type():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    payload = {
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "location": "中东",
                            "region": "西亚",
                            "name": "中东",
                            "description": "地区",
                        }
                    ]
                }
            ]
        }
    }
    original = json.dumps(payload, ensure_ascii=False)
    response = SimpleNamespace(content=original)

    assert _normalize_extraction_response(response, {"location", "region"}) == 0
    assert response.content == original


def test_extraction_response_does_not_invent_unknown_entity_type():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    payload = {
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "unknown": "中东",
                            "name": "中东",
                            "description": "地区",
                        }
                    ]
                }
            ]
        }
    }
    original = json.dumps(payload, ensure_ascii=False)
    response = SimpleNamespace(content=original)

    assert _normalize_extraction_response(response, {"location"}) == 0
    assert response.content == original


def test_extraction_response_downgrades_overflow_integer_entity_value():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    response = SimpleNamespace(
        content=json.dumps(
            {
                "data": {
                    "items": [
                        {
                            "entities": [
                                {
                                    "type": "metric",
                                    "name": "累计交易笔数",
                                    "description": "统计指标",
                                    "value_type": "int",
                                    "value": "9223372036854775808",
                                }
                            ]
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
    )

    assert _normalize_extraction_response(response, {"metric"}) == 1
    entity = json.loads(response.content)["data"]["items"][0]["entities"][0]
    assert entity["value_type"] == "text"
    assert entity["value"] == "9223372036854775808"


def test_extraction_response_keeps_sqlite_integer_boundaries():
    import json

    from sag_api.sag.incremental_processor import _normalize_extraction_response

    values = ["-9223372036854775808", "9223372036854775807"]
    response = SimpleNamespace(
        content=json.dumps(
            {
                "data": {
                    "items": [
                        {
                            "entities": [
                                {
                                    "type": "metric",
                                    "name": value,
                                    "description": "统计指标",
                                    "value_type": "int",
                                    "value": value,
                                }
                                for value in values
                            ]
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
    )

    assert _normalize_extraction_response(response, {"metric"}) == 0
    entities = json.loads(response.content)["data"]["items"][0]["entities"]
    assert [entity["value_type"] for entity in entities] == ["int", "int"]


def test_upstream_entity_value_parser_downgrades_overflow_integer():
    from zleap.sag.modules.extract.parser import EntityValueParser

    from sag_api.sag.incremental_processor import _install_sqlite_integer_guard

    _install_sqlite_integer_guard()
    fields = EntityValueParser().parse_to_typed_fields("9223372036854775808笔", entity_type="metric")

    assert fields["value_type"] == "text"
    assert fields["int_value"] is None


@pytest.mark.asyncio
async def test_extract_chunk_downgrades_overflow_integer_before_sag_persists(monkeypatch):
    import json

    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    payload = {
        "data": {
            "items": [
                {
                    "entities": [
                        {
                            "type": "metric",
                            "name": "9223372036854775808笔",
                            "description": "累计交易笔数",
                        }
                    ]
                }
            ]
        }
    }

    class FakeLeafClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(
                content=json.dumps(payload, ensure_ascii=False),
                usage=SimpleNamespace(total_tokens=42),
            )

    class FakeRetryClient:
        def __init__(self):
            self.client = FakeLeafClient()

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeRetryClient()

        async def _get_llm_client(self):
            return self.client

        async def extract(self, config):
            request = {"data": {"meta": {"entity_types": [{"type": "metric", "description": "指标"}]}}}
            response = await self.client.client.chat([SimpleNamespace(content=json.dumps(request, ensure_ascii=False))])
            entity = json.loads(response.content)["data"]["items"][0]["entities"][0]
            assert entity["value_type"] == "text"
            return [SimpleNamespace(id="event-1")]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    assert await processor._extract_chunk("chunk-1") == (["event-1"], 42)


@pytest.mark.asyncio
async def test_extract_chunk_raises_when_sag_swallows_chunk_failure(monkeypatch):
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=23))

    class SwallowingExtractor:
        def __init__(self, **kwargs):
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def extract_from_chunk(self, chunk, config):
            await self.client.chat([SimpleNamespace(content="book")])
            raise RuntimeError("response schema is invalid")

        async def extract(self, config):
            try:
                await self.extract_from_chunk(SimpleNamespace(id=config.chunk_ids[0]), config)
            except Exception:
                # Mirrors zleap-sag 0.7.x: the batch helper logs a chunk failure
                # and returns an empty event list instead of propagating it.
                return []
            raise AssertionError("expected the fake chunk to fail")

    monkeypatch.setattr(processor_module, "EventExtractor", SwallowingExtractor)
    engine = SimpleNamespace(_extractor=SimpleNamespace(prompt_manager=object(), model_config={}))
    processor = IncrementalDocumentProcessor(engine, "source-config", max_concurrency=1)

    with pytest.raises(RuntimeError, match="response schema is invalid"):
        await processor._extract_chunk("chunk-1")


@pytest.mark.asyncio
async def test_extract_chunk_retries_event_without_entity_before_save(monkeypatch):
    """Persisting the first invalid Event would violate the OCTX Event→Entity contract."""
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=10))

    class FakeExtractor:
        created = 0
        saved: list[str] = []

        def __init__(self, **kwargs):
            self.attempt = FakeExtractor.created
            FakeExtractor.created += 1
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def _save_events(self, events, config):
            FakeExtractor.saved.extend(event.id for event in events)
            return events

        async def extract(self, config):
            await self.client.chat([SimpleNamespace(content="knowledge")])
            event = SimpleNamespace(
                id=f"event-{self.attempt}",
                content="Event body",
                event_associations=[] if self.attempt == 0 else [object()],
                children=[],
            )
            await self._save_events([event], config)
            return [event]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(
        _extractor=SimpleNamespace(prompt_manager=object(), model_config={})
    )
    processor = IncrementalDocumentProcessor(
        engine,
        "source-config",
        max_concurrency=1,
        event_entity_attempts=2,
    )

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 20
    assert FakeExtractor.saved == ["event-1"]


@pytest.mark.asyncio
async def test_extract_chunk_retries_event_without_content_before_save(monkeypatch):
    """An Event with entities but no body must never cross the persistence boundary."""
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=10))

    class FakeExtractor:
        created = 0
        saved: list[str] = []

        def __init__(self, **kwargs):
            self.attempt = FakeExtractor.created
            FakeExtractor.created += 1
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def _save_events(self, events, config):
            FakeExtractor.saved.extend(event.id for event in events)
            return events

        async def extract(self, config):
            await self.client.chat([SimpleNamespace(content="knowledge")])
            event = SimpleNamespace(
                id=f"event-{self.attempt}",
                content="   " if self.attempt == 0 else "Event body",
                event_associations=[object()],
                children=[],
            )
            await self._save_events([event], config)
            return [event]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(
        _extractor=SimpleNamespace(prompt_manager=object(), model_config={})
    )
    processor = IncrementalDocumentProcessor(
        engine,
        "source-config",
        max_concurrency=1,
        event_entity_attempts=2,
    )

    event_ids, token_usage = await processor._extract_chunk("chunk-1")

    assert event_ids == ["event-1"]
    assert token_usage == 20
    assert FakeExtractor.saved == ["event-1"]


@pytest.mark.asyncio
async def test_extract_chunk_becomes_eventless_after_entity_contract_exhaustion(
    monkeypatch,
):
    """Repeated invalid Events must yield an eventless Chunk, never an incomplete graph."""
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=7))

    class FakeExtractor:
        created = 0
        save_calls = 0

        def __init__(self, **kwargs):
            self.attempt = FakeExtractor.created
            FakeExtractor.created += 1
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def _save_events(self, events, config):
            FakeExtractor.save_calls += 1
            return events

        async def extract(self, config):
            await self.client.chat([SimpleNamespace(content="knowledge")])
            event = SimpleNamespace(
                id=f"event-{self.attempt}",
                event_associations=[],
                children=[],
            )
            await self._save_events([event], config)
            return [event]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    engine = SimpleNamespace(
        _extractor=SimpleNamespace(prompt_manager=object(), model_config={})
    )
    processor = IncrementalDocumentProcessor(
        engine,
        "source-config",
        max_concurrency=1,
        event_entity_attempts=2,
    )

    assert await processor._extract_chunk("chunk-1") == ([], 14)
    assert FakeExtractor.created == 2
    assert FakeExtractor.save_calls == 0


def test_event_entity_schema_is_strengthened_without_mutating_prompt_schema():
    """Mutating the shared prompt schema would leak one request's policy into others."""
    from sag_api.sag.incremental_processor import _strengthen_event_entity_schema

    original = {
        "definitions": {
            "event": {
                "type": "object",
                "required": ["title"],
                "properties": {
                    "entities": {"type": "array", "items": {}},
                    "content": {"type": "string"},
                },
            }
        }
    }

    strengthened = _strengthen_event_entity_schema(original)

    assert original["definitions"]["event"]["required"] == ["title"]
    event = strengthened["definitions"]["event"]
    assert event["required"] == ["title", "entities", "content"]
    assert event["properties"]["entities"]["minItems"] == 1
    assert event["properties"]["content"]["minLength"] == 1


def test_event_entity_attempt_setting_is_bounded():
    """An unbounded contract retry would amplify LLM cost and stall document jobs."""
    from pydantic import ValidationError

    from sag_api.core.config import Settings

    assert Settings(_env_file=None).document_event_entity_attempts == 2
    assert Settings(
        _env_file=None, document_event_entity_attempts=3
    ).document_event_entity_attempts == 3
    with pytest.raises(ValidationError):
        Settings(_env_file=None, document_event_entity_attempts=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, document_event_entity_attempts=4)


@pytest.mark.asyncio
async def test_extract_chunk_passes_strengthened_schema_to_llm(monkeypatch):
    """A weak provider path must receive the strongest schema SAG can express."""
    from sag_api.sag import incremental_processor as processor_module
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    class FakeClient:
        async def chat(self, messages, **kwargs):
            return SimpleNamespace(content="{}", usage=SimpleNamespace(total_tokens=3))

        async def chat_with_schema(self, messages, *, response_schema):
            event = response_schema["definitions"]["event"]
            assert "entities" in event["required"]
            assert "content" in event["required"]
            assert event["properties"]["entities"]["minItems"] == 1
            assert event["properties"]["content"]["minLength"] == 1
            return await self.chat(messages)

    class FakeExtractor:
        def __init__(self, **kwargs):
            self.client = FakeClient()

        async def _get_llm_client(self):
            return self.client

        async def _save_events(self, events, config):
            return events

        async def extract(self, config):
            schema = {
                "definitions": {
                        "event": {
                            "required": ["title"],
                            "properties": {
                                "entities": {"type": "array"},
                                "content": {"type": "string"},
                            },
                        }
                }
            }
            await self.client.chat_with_schema([], response_schema=schema)
            event = SimpleNamespace(
                id="event-1",
                content="Event body",
                event_associations=[object()],
                children=[],
            )
            await self._save_events([event], config)
            return [event]

    monkeypatch.setattr(processor_module, "EventExtractor", FakeExtractor)
    processor = IncrementalDocumentProcessor(
        SimpleNamespace(
            _extractor=SimpleNamespace(prompt_manager=object(), model_config={})
        ),
        "source-config",
        max_concurrency=1,
    )

    assert await processor._extract_chunk("chunk-1") == (["event-1"], 3)


@pytest.mark.asyncio
async def test_entity_contract_exhaustion_is_persisted_in_checkpoint(monkeypatch):
    """Without durable diagnostics, an eventless quality fallback is indistinguishable from no Event."""
    from sag_api.sag.dto import ProcessCheckpoint
    from sag_api.sag.incremental_processor import IncrementalDocumentProcessor

    processor = IncrementalDocumentProcessor(
        SimpleNamespace(_extractor=object()),
        "source-config",
        max_concurrency=1,
    )
    processor._event_entity_rejection_counts = {"chunk-1": 2}

    async def exhausted(_chunk_id):
        return [], 14

    async def publish(checkpoint):
        snapshots.append(checkpoint)

    snapshots: list[ProcessCheckpoint] = []
    monkeypatch.setattr(processor, "_extract_chunk", exhausted)
    await processor._extract_remaining(
        ["chunk-1"],
        current=ProcessCheckpoint(chunk_ids=["chunk-1"]),
        on_checkpoint=publish,
        should_pause=_return_false,
    )

    quality = snapshots[-1].event_entity_quality
    assert quality.rejected_attempts == 2
    assert quality.eventless_after_contract == ["chunk-1"]
    assert quality.reason_counts == {"entities_missing": 2}


async def _return_false():
    return False


async def _append_checkpoint(snapshots, value):
    snapshots.append(value)


@pytest.mark.asyncio
async def test_pause_and_resume_document_service():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="resume-source", sag_source_config_id="resume-source-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume.md",
            status=DocumentStatus.EXTRACTING,
            progress=52,
            token_usage=12_000,
        )
        session.add(document)
        await session.flush()
        job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.RUNNING,
            source_id=source.id,
            document_id=document.id,
            progress=0.52,
            payload={
                "process_checkpoint": {
                    "source_id": "engine-source",
                    "chunk_ids": ["c1", "c2"],
                    "processed_chunk_ids": ["c1"],
                    "event_count": 1,
                    "event_ids": ["e1"],
                    "token_usage": 12_000,
                }
            },
        )
        session.add(job)
        await session.commit()

        paused_job = await pause_document(session, source, document.id)
        assert paused_job.status == JobStatus.PAUSED
        await session.refresh(document)
        assert document.status == DocumentStatus.PAUSING

        document.status = DocumentStatus.PAUSED
        await session.commit()

        queue = FakeQueue()
        resumed_job = await resume_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        assert resumed_job.status == JobStatus.QUEUED
        assert resumed_job.payload["resume_requested"] is True
        assert "pause_requested" not in resumed_job.payload
        assert resumed_job.payload["_scheduler"]["priority"] == 10
        assert document.status == DocumentStatus.EXTRACTING
        assert document.progress == 52 and document.token_usage == 12_000
        assert queue.ids == [job.id]

        queued_document = Document(
            source_id=source.id,
            filename="queued.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/queued.md",
            status=DocumentStatus.PENDING,
        )
        session.add(queued_document)
        await session.flush()
        queued_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            status=JobStatus.QUEUED,
            source_id=source.id,
            document_id=queued_document.id,
        )
        session.add(queued_job)
        await session.commit()

        stopped_before_start = await pause_document(session, source, queued_document.id)
        assert stopped_before_start.status == JobStatus.PAUSED
        assert queued_document.status == DocumentStatus.PAUSED


@pytest.mark.asyncio
async def test_delete_document_persists_deleting_and_queues_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="delete-source", sag_source_config_id="delete-source-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()

        queue = FakeQueue()
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.payload["pause_requested"] is True
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert delete_job.payload["_scheduler"]["priority"] == 0
        assert queue.ids == [delete_job.id]
        assert queue.maintenance == [(source.id, delete_job.id)]

        # Idempotent retries must also repair a stale visible state instead of
        # returning an active delete job while the document appears extracting.
        document.status = DocumentStatus.EXTRACTING
        await session.commit()
        repeated = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        await session.refresh(document)
        assert repeated.id == delete_job.id
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_concurrent_delete_requests_share_one_cleanup_job():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-delete-request",
            sag_source_config_id="concurrent-delete-request-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="extracting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/extracting.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-extracting",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    queue = FakeQueue()

    async def remove():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await delete_document(
                session,
                source,
                document_id,
                job_queue=queue,
            )

    first, second = await asyncio.gather(remove(), remove())

    async with SessionLocal() as session:
        jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.DELETE_DOCUMENT,
                    )
                )
            ).all()
        )
    assert first.id == second.id
    assert [job.id for job in jobs] == [first.id]
    assert set(queue.ids) == {first.id}
    assert set(queue.maintenance) == {(source_id, first.id)}


@pytest.mark.asyncio
async def test_pause_and_resume_only_control_process_jobs():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document, resume_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            raise AssertionError("delete jobs must never be resumed as extraction jobs")

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="action-guards", sag_source_config_id="action-guards-config")
        session.add(source)
        await session.flush()
        deleting = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        paused = Document(
            source_id=source.id,
            filename="paused.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/paused.md",
            status=DocumentStatus.PAUSED,
        )
        session.add_all([deleting, paused])
        await session.flush()
        running_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=deleting.id,
            status=JobStatus.RUNNING,
        )
        paused_delete = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=paused.id,
            status=JobStatus.PAUSED,
        )
        session.add_all([running_delete, paused_delete])
        await session.commit()

        with pytest.raises(ConflictError):
            await pause_document(session, source, deleting.id)
        with pytest.raises(ConflictError):
            await resume_document(session, source, paused.id, job_queue=FakeQueue())

        await session.refresh(running_delete)
        await session.refresh(paused_delete)
        assert running_delete.status == JobStatus.RUNNING
        assert "pause_requested" not in running_delete.payload
        assert paused_delete.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_delete_document_job_removes_document_after_processing_stops(tmp_path, monkeypatch):
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.services import universe_service

    class FakeEngine:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, _config_id, document_source_id, *, source):
            assert source.sag_source_config_id == "delete-worker-config"
            self.deleted.append(document_source_id)

    await init_db()

    async def partially_scheduled_refresh(session, _job_queue, *, source_id, reason):
        session.add(
            Job(
                type=JobType.INDEX_UNIVERSE,
                source_id=source_id,
                status=JobStatus.QUEUED,
                payload={"reason": reason},
            )
        )
        await session.flush()
        raise RuntimeError("refresh scheduling failed")

    monkeypatch.setattr(universe_service, "schedule_universe_refresh", partially_scheduled_refresh)
    path = tmp_path / "deleting.md"
    path.write_text("content")
    async with SessionLocal() as session:
        source = Source(name="delete-worker", sag_source_config_id="delete-worker-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=7,
            storage_path=str(path),
            status=DocumentStatus.DELETING,
            sag_source_id="engine-document",
        )
        session.add(document)
        await session.flush()
        other_document = Document(
            source_id=source.id,
            filename="keep.md",
            content_type="text/markdown",
            size_bytes=4,
            storage_path=str(tmp_path / "keep.md"),
            status=DocumentStatus.EXTRACTING,
        )
        session.add(other_document)
        await session.flush()
        blocked_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=other_document.id,
            status=JobStatus.QUEUED,
            payload={
                "_scheduler": {
                    "priority": 50,
                    "blocked_reason": "source_maintenance",
                }
            },
        )
        session.add(blocked_job)
        delete_job = Job(
            type=JobType.DELETE_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.QUEUED,
        )
        session.add(delete_job)
        await session.commit()
        document_id, delete_job_id, blocked_job_id = document.id, delete_job.id, blocked_job.id
        source_id = source.id

    engine = FakeEngine()
    queue = InProcessAsyncQueue(SessionLocal, engine, concurrency=1)
    queue.begin_source_maintenance(source_id, delete_job_id)
    await queue._run(delete_job_id)

    async with SessionLocal() as session:
        assert await session.get(Document, document_id) is None
        completed_delete = await session.get(Job, delete_job_id)
        assert completed_delete is not None
        assert completed_delete.status == JobStatus.SUCCEEDED
        assert completed_delete.document_id is None
        assert completed_delete.payload["target_document_id"] == document_id
        source = await session.scalar(select(Source).where(Source.name == "delete-worker"))
        assert source is not None and source.document_count == 1
        resumed = await session.get(Job, blocked_job_id)
        assert resumed.status == JobStatus.QUEUED
        assert resumed.payload["resume_requested"] is True
        assert resumed.payload["_scheduler"] == {"priority": 10}
        universe_jobs = list(
            (
                await session.scalars(
                    select(Job).where(Job.type == JobType.INDEX_UNIVERSE)
                )
            ).all()
        )
        assert universe_jobs == []
    assert engine.deleted == ["engine-document"]
    assert not path.exists()
    assert queue.source_maintenance_requested(source_id) is False
    queued_ids: list[str] = []
    while not queue._queue.empty():
        queued_ids.append((await queue._queue.get())[-1])
    assert blocked_job_id in queued_ids


@pytest.mark.asyncio
async def test_reprocess_ready_document_replaces_all_previous_derived_data():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            raise AssertionError("reprocess request must not wait for engine cleanup")

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="replace-source",
            sag_source_config_id="replace-source-config",
            document_count=2,
            chunk_count=99,
            event_count=88,
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="book.txt",
            content_type="text/plain",
            size_bytes=100,
            storage_path="/tmp/book.txt",
            status=DocumentStatus.READY,
            progress=100,
            chunk_count=3,
            event_count=2,
            token_usage=500,
            sag_source_id="engine-latest",
            parser_provider="mineru",
            mineru_provider="official",
            mineru_model="pipeline",
            parser_status="done",
            fallback_from="mineru",
            fallback_reason="previous fallback",
        )
        other = Document(
            source_id=source.id,
            filename="other.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/other.md",
            status=DocumentStatus.READY,
            progress=100,
            chunk_count=4,
            event_count=5,
            sag_source_id="engine-other",
        )
        session.add_all([document, other])
        await session.flush()
        session.add_all(
            [
                Job(
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.SUCCEEDED,
                    source_id=source.id,
                    document_id=document.id,
                    payload={"process_checkpoint": {"source_id": "engine-old"}},
                ),
                Job(
                    type=JobType.PROCESS_DOCUMENT,
                    status=JobStatus.SUCCEEDED,
                    source_id=source.id,
                    document_id=document.id,
                    payload={"process_checkpoint": {"source_id": "engine-latest"}},
                ),
            ]
        )
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        job = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        assert engine.deleted == []
        assert document.status == DocumentStatus.PENDING
        assert document.progress == 0
        assert document.chunk_count == 0 and document.event_count == 0
        assert document.token_usage == 0 and document.sag_source_id is None
        assert document.parser_provider is None
        assert document.mineru_provider is None and document.mineru_model is None
        assert document.parser_status is None
        assert document.fallback_from is None and document.fallback_reason is None
        assert source.document_count == 2
        assert source.chunk_count == 4 and source.event_count == 5
        assert job.type == JobType.REPROCESS_DOCUMENT
        assert job.payload["target_document_id"] == document.id
        assert set(job.payload["derived_source_ids"]) == {"engine-old", "engine-latest"}
        assert job.payload["_scheduler"]["priority"] == 0
        assert queue.ids == [job.id]
        assert queue.maintenance == [(source.id, job.id)]

        # Source rows are shared across this module's SQLite test database; do
        # not leave a freshly reprocessed source that would make universe-cache
        # contract tests correctly report their manifest as stale.
        await session.delete(source)
        await session.commit()


@pytest.mark.asyncio
async def test_reprocess_cleanup_job_deletes_old_data_before_queuing_processing():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.tasks import reprocess_document_task

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, source_config_id, document_source_id, *, source):
            assert source_config_id == source.sag_source_config_id
            self.deleted.append(document_source_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="reprocess-worker", sag_source_config_id="reprocess-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.PENDING,
        )
        session.add(document)
        await session.flush()
        cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old", "engine-latest", "engine-old"],
            },
        )
        session.add(cleanup)
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )

        process_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document.id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        assert engine.deleted == ["engine-latest", "engine-old"]
        assert len(process_jobs) == 1
        assert process_jobs[0].status == JobStatus.QUEUED
        assert process_jobs[0].payload == {}
        assert cleanup.payload["cleanup_completed"] is True
        assert queue.ids == [process_jobs[0].id, process_jobs[0].id]


@pytest.mark.asyncio
async def test_reprocess_cleanup_does_not_queue_processing_after_delete_request():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.tasks import reprocess_document_task

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

    class FakeEngineManager:
        def __init__(self):
            self.deleted: list[str] = []

        async def delete_document_data(self, _config_id, document_source_id, *, source):
            assert source.sag_source_config_id
            self.deleted.append(document_source_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-during-reprocess-cleanup",
            sag_source_config_id="delete-during-reprocess-cleanup-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="deleting.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/deleting.md",
            status=DocumentStatus.DELETING,
        )
        session.add(document)
        await session.flush()
        cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old"],
            },
        )
        session.add(cleanup)
        await session.commit()

        queue = FakeQueue()
        engine = FakeEngineManager()
        await reprocess_document_task(
            session,
            cleanup,
            engine_manager=engine,
            job_queue=queue,
        )

        process_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document.id,
                        Job.type == JobType.PROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        assert engine.deleted == ["engine-old"]
        assert process_jobs == []
        assert cleanup.payload["cleanup_completed"] is True
        assert queue.ids == []


@pytest.mark.asyncio
async def test_concurrent_reprocess_requests_share_one_cleanup_job():
    from sqlalchemy import select

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="concurrent-reprocess",
            sag_source_config_id="concurrent-reprocess-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            progress=100,
            sag_source_id="engine-ready",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED,
                payload={"process_checkpoint": {"source_id": "engine-ready"}},
            )
        )
        await session.commit()
        source_id, document_id = source.id, document.id

    queue = FakeQueue()

    async def retry():
        async with SessionLocal() as session:
            source = await session.get(Source, source_id)
            return await reprocess_document(
                session,
                source,
                document_id,
                job_queue=queue,
            )

    first, second = await asyncio.gather(retry(), retry())

    async with SessionLocal() as session:
        cleanup_jobs = list(
            (
                await session.scalars(
                    select(Job).where(
                        Job.document_id == document_id,
                        Job.type == JobType.REPROCESS_DOCUMENT,
                    )
                )
            ).all()
        )
        document = await session.get(Document, document_id)
        assert document.status == DocumentStatus.PENDING
    assert first.id == second.id
    assert [job.id for job in cleanup_jobs] == [first.id]
    assert queue.ids == [first.id]
    assert queue.maintenance == [(source_id, first.id)]


@pytest.mark.asyncio
async def test_retrying_failed_reprocess_cleanup_stays_in_maintenance_flow():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="failed-reprocess-cleanup",
            sag_source_config_id="failed-reprocess-cleanup-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="failed.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            error="cleanup failed",
        )
        session.add(document)
        await session.flush()
        failed_cleanup = Job(
            type=JobType.REPROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.FAILED,
            payload={
                "target_document_id": document.id,
                "derived_source_ids": ["engine-old"],
                "_scheduler": {"priority": 0},
            },
            error="cleanup failed",
        )
        session.add(failed_cleanup)
        await session.commit()

        queue = FakeQueue()
        retried = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        assert retried.type == JobType.REPROCESS_DOCUMENT
        assert retried.status == JobStatus.QUEUED
        assert retried.payload["derived_source_ids"] == ["engine-old"]
        assert document.status == DocumentStatus.PENDING
        assert document.error is None
        assert queue.ids == [retried.id]
        assert queue.maintenance == [(source.id, retried.id)]


@pytest.mark.asyncio
async def test_delete_after_reprocess_request_uses_maintenance_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, reprocess_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []
            self.maintenance: list[tuple[str, str]] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, source_id: str, job_id: str):
            self.maintenance.append((source_id, job_id))

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="delete-after-reprocess",
            sag_source_config_id="delete-after-reprocess-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="ready.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/ready.md",
            status=DocumentStatus.READY,
            progress=100,
            sag_source_id="engine-ready",
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.SUCCEEDED,
                payload={"process_checkpoint": {"source_id": "engine-ready"}},
            )
        )
        await session.commit()

        queue = FakeQueue()
        reprocess_job = await reprocess_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )
        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING
        assert reprocess_job.status == JobStatus.QUEUED
        assert delete_job.type == JobType.DELETE_DOCUMENT
        assert delete_job.status == JobStatus.QUEUED
        assert await session.get(Document, document.id) is not None
        assert queue.maintenance == [
            (source.id, reprocess_job.id),
            (source.id, delete_job.id),
        ]


@pytest.mark.asyncio
async def test_delete_after_failed_reprocess_keeps_old_engine_ids_for_cleanup():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(name="failed-reprocess-delete", sag_source_config_id="failed-config")
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="failed.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/failed.md",
            status=DocumentStatus.FAILED,
            sag_source_id=None,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.REPROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.FAILED,
                payload={
                    "target_document_id": document.id,
                    "derived_source_ids": ["engine-old", "engine-older"],
                },
            )
        )
        await session.commit()

        delete_job = await delete_document(
            session,
            source,
            document.id,
            job_queue=FakeQueue(),
        )

        assert set(delete_job.payload["derived_source_ids"]) == {
            "engine-old",
            "engine-older",
        }


@pytest.mark.asyncio
async def test_job_pause_is_not_failure_or_retry(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobPaused()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=1)
    await queue._run(job_id)
    async with SessionLocal() as session:
        paused = await session.get(Job, job_id)
        assert paused.status == JobStatus.PAUSED
        assert paused.attempts == 1
        assert paused.error is None
        paused.status = JobStatus.QUEUED
        paused.payload = {**(paused.payload or {}), "resume_requested": True}
        paused.progress = 0.4
        await session.commit()

    await queue._run(job_id)
    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 2


@pytest.mark.asyncio
async def test_duplicate_queue_entries_claim_job_once(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Job
    from sag_api.enums import JobStatus, JobType
    from sag_api.jobs.inproc import InProcessAsyncQueue
    from sag_api.jobs.tasks import TASK_HANDLERS

    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_session, _job, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    monkeypatch.setitem(TASK_HANDLERS, JobType.PROCESS_DOCUMENT, handler)
    await init_db()
    async with SessionLocal() as session:
        job = Job(type=JobType.PROCESS_DOCUMENT, status=JobStatus.QUEUED)
        session.add(job)
        await session.commit()
        job_id = job.id

    queue = InProcessAsyncQueue(SessionLocal, engine_manager=None, concurrency=2)
    first = asyncio.create_task(queue._run(job_id))
    second = asyncio.create_task(queue._run(job_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)

    async with SessionLocal() as session:
        done = await session.get(Job, job_id)
        assert done.status == JobStatus.SUCCEEDED
        assert done.attempts == 1
        assert calls == 1


@pytest.mark.asyncio
async def test_pause_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, pause_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-delete-cas",
            sag_source_config_id="pause-delete-cas-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-delete.md",
            status=DocumentStatus.EXTRACTING,
            sag_source_id="engine-pause-delete",
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if (
                not delete_injected
                and isinstance(result, Job)
                and result.id == process_job_id
            ):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await pause_document(session, source, document_id)

        await session.refresh(document)
        assert document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_cannot_overwrite_delete_that_commits_after_job_lookup(monkeypatch):
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import delete_document, resume_document

    class FakeQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, job_id: str):
            self.ids.append(job_id)

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-delete-cas",
            sag_source_config_id="resume-delete-cas-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-delete.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
            payload={"process_checkpoint": {"source_id": "engine-resume-delete"}},
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id, process_job_id = (
            source.id,
            document.id,
            process_job.id,
        )

        queue = FakeQueue()
        real_scalar = session.scalar
        delete_injected = False

        async def scalar_then_delete(statement, *args, **kwargs):
            nonlocal delete_injected
            result = await real_scalar(statement, *args, **kwargs)
            if (
                not delete_injected
                and isinstance(result, Job)
                and result.id == process_job_id
            ):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=queue,
                    )
            return result

        monkeypatch.setattr(session, "scalar", scalar_then_delete)

        with pytest.raises(ConflictError, match="删除|状态"):
            await resume_document(session, source, document_id, job_queue=queue)

        await session.refresh(document)
        await session.refresh(process_job)
        assert document.status == DocumentStatus.DELETING
        assert process_job.status == JobStatus.PAUSED


@pytest.mark.asyncio
async def test_pause_rejects_ready_document_while_job_completion_is_committing():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.core.errors import ConflictError
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import pause_document

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="pause-finish-cas",
            sag_source_config_id="pause-finish-cas-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="pause-finish.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/pause-finish.md",
            status=DocumentStatus.READY,
            progress=100,
        )
        session.add(document)
        await session.flush()
        session.add(
            Job(
                type=JobType.PROCESS_DOCUMENT,
                source_id=source.id,
                document_id=document.id,
                status=JobStatus.RUNNING,
                progress=0.99,
            )
        )
        await session.commit()

        with pytest.raises(ConflictError, match="结束|状态"):
            await pause_document(session, source, document.id)


@pytest.mark.parametrize("engine_mode", ["complete", "paused", "error"])
@pytest.mark.asyncio
async def test_process_exit_cannot_overwrite_concurrent_delete(
    monkeypatch,
    engine_mode,
):
    from types import SimpleNamespace

    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.jobs.control import JobPaused
    from sag_api.jobs.tasks import process_document
    from sag_api.services.document_service import delete_document

    class FakeEngine:
        async def process_document(self, *_args, **_kwargs):
            if engine_mode == "error":
                raise RuntimeError("inflight extraction failed")
            return SimpleNamespace(
                paused=engine_mode == "paused",
                chunk_count=1,
                event_count=1,
                source_id="engine-exit-delete",
                token_usage=50,
            )

    class FakeQueue:
        async def enqueue(self, _job_id: str):
            return None

        def begin_source_maintenance(self, _source_id: str, _job_id: str):
            return None

        def source_maintenance_requested(self, _source_id: str):
            return False

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name=f"{engine_mode}-delete-cas",
            sag_source_config_id=f"{engine_mode}-delete-cas-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename=f"{engine_mode}-delete.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path=f"/tmp/{engine_mode}-delete.md",
            status=DocumentStatus.EXTRACTING,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.RUNNING,
            payload={
                "process_checkpoint": {
                    "source_id": "engine-exit-delete",
                    "chunk_ids": ["chunk-1"],
                    "processed_chunk_ids": [],
                }
            },
        )
        session.add(process_job)
        await session.commit()
        source_id, document_id = source.id, document.id

        real_refresh = session.refresh
        delete_injected = False

        async def refresh_then_delete(instance, *args, **kwargs):
            nonlocal delete_injected
            await real_refresh(instance, *args, **kwargs)
            if not delete_injected and isinstance(instance, Document):
                delete_injected = True
                await session.commit()
                async with SessionLocal() as delete_session:
                    deleting_source = await delete_session.get(Source, source_id)
                    await delete_document(
                        delete_session,
                        deleting_source,
                        document_id,
                        job_queue=FakeQueue(),
                    )

        monkeypatch.setattr(session, "refresh", refresh_then_delete)

        with pytest.raises(JobPaused):
            await process_document(
                session,
                process_job,
                engine_manager=FakeEngine(),
                job_queue=FakeQueue(),
            )

        async with SessionLocal() as verification_session:
            saved_document = await verification_session.get(Document, document_id)
            assert saved_document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_resume_uses_supervised_dispatch_after_persisting_queued_state():
    from sag_api.core.db import SessionLocal, init_db
    from sag_api.db.models import Document, Job, Source
    from sag_api.enums import DocumentStatus, JobStatus, JobType
    from sag_api.services.document_service import resume_document

    class DurableQueue:
        def __init__(self):
            self.ids: list[str] = []

        async def enqueue(self, _job_id: str):
            raise AssertionError("persisted resume must use supervised dispatch")

        async def enqueue_durably(self, job_id: str):
            self.ids.append(job_id)

    await init_db()
    async with SessionLocal() as session:
        source = Source(
            name="resume-durable-dispatch",
            sag_source_config_id="resume-durable-dispatch-config",
        )
        session.add(source)
        await session.flush()
        document = Document(
            source_id=source.id,
            filename="resume-durable-dispatch.md",
            content_type="text/markdown",
            size_bytes=10,
            storage_path="/tmp/resume-durable-dispatch.md",
            status=DocumentStatus.PAUSED,
        )
        session.add(document)
        await session.flush()
        process_job = Job(
            type=JobType.PROCESS_DOCUMENT,
            source_id=source.id,
            document_id=document.id,
            status=JobStatus.PAUSED,
        )
        session.add(process_job)
        await session.commit()

        queue = DurableQueue()
        resumed = await resume_document(
            session,
            source,
            document.id,
            job_queue=queue,
        )

        assert resumed.status == JobStatus.QUEUED
        assert queue.ids == [process_job.id]
