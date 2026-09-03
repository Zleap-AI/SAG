from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from octx import create_octx, open_octx, validate_octx

from sag_api.sag.octx_vector_protocol import (
    configured_embedding_identity,
    input_sha256,
    render_role_input,
    write_existing_vector_payload,
)
from sag_api.sag.octx_vector_reuse import (
    ArrowVectorReuseReader,
    get_reused_vector,
    prepare_vector_reuse,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def _transport_only_vector_package(tmp_path: Path) -> Path:
    import pyarrow as pa
    import pyarrow.ipc as ipc

    document_id = "019c1234-5678-7abc-8def-0123456789ab"
    chunk_id = "019c2222-2222-7222-8222-222222222222"
    source = tmp_path / "transport-source"
    source.mkdir()
    (source / "guide.md").write_text(
        f"---\ntype: Reference\ntitle: Guide\noctx:\n  document_id: {document_id}\n---\n# Guide\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "transport-workspace"
    create_octx(workspace, source=source, name="Transport vectors", output=tmp_path / "transport-base.octx")
    _write_jsonl(
        workspace / "data/chunks.jsonl",
        [{"id": chunk_id, "document_id": document_id, "ordinal": 0, "heading": "Head", "text": "Body"}],
    )
    _write_jsonl(workspace / "data/events.jsonl", [])
    _write_jsonl(workspace / "data/entities.jsonl", [])
    _write_jsonl(workspace / "relations/chunk-events.jsonl", [])
    _write_jsonl(workspace / "relations/event-entities.jsonl", [])
    vectors = workspace / "vectors"
    vectors.mkdir()
    profile = {
        "role": "chunk.content",
        "reuse_policy": "rebuild_required",
        "dimensions": 3,
        "dtype": "float32",
        "coverage": "complete",
        "recipe_id": "sag.chunk-content/1",
        "recipe": {
            "fields": ["heading", "text"],
            "separator": "\n\n",
            "encoding": "UTF-8",
            "unicode_normalization": "NFC",
            "newline": "LF",
            "null_as": "",
            "trim": False,
        },
        "input_hash_algorithm": "sha256",
    }
    (vectors / "profiles.json").write_text(json.dumps({"profiles": [profile]}) + "\n", encoding="utf-8")
    schema = pa.schema(
        [
            pa.field("record_id", pa.string(), nullable=False),
            pa.field("input_sha256", pa.string(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), 3), nullable=False),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([chunk_id]),
            pa.array([input_sha256("Head\n\nBody")]),
            pa.array([[0.1, 0.2, 0.3]], type=pa.list_(pa.float32(), 3)),
        ],
        schema=schema,
    )
    with pa.OSFile(str(vectors / "chunk_content.arrow"), "wb") as output:
        with ipc.new_file(output, schema) as writer:
            writer.write_table(table)
    return create_octx(
        workspace,
        version="1.1.0",
        output=tmp_path / "transport-only.octx",
        capabilities={"sag-structured": "0.1", "vectors": "0.1"},
    ).output


def test_role_input_is_canonical_across_unicode_and_newlines() -> None:
    record = {"heading": "Cafe\u0301\r\nTitle", "text": "Body\rLine"}

    rendered = render_role_input("chunk.content", record)

    assert rendered == "Café\nTitle\n\nBody\nLine"
    assert input_sha256(rendered) == input_sha256("Café\nTitle\n\nBody\nLine")


def test_embedding_identity_requires_an_explicit_dimension() -> None:
    configured = SimpleNamespace(
        embedding_model="test/embedding",
        effective_embedding_base_url="https://embedding.invalid/v1",
        embedding_dimensions=3,
    )
    unknown = SimpleNamespace(
        embedding_model="test/embedding",
        effective_embedding_base_url="https://embedding.invalid/v1",
        embedding_dimensions=None,
    )

    assert configured_embedding_identity(configured)["dimensions"] == 3
    assert configured_embedding_identity(unknown) is None


def test_embedding_identity_distinguishes_service_endpoints_without_exposing_them() -> None:
    from sag_api.sag.octx_vector_protocol import embedding_identity

    first = SimpleNamespace(model="test/embedding", base_url="https://one.invalid/v1", dimensions=3)
    second = SimpleNamespace(model="test/embedding", base_url="https://two.invalid/v1", dimensions=3)

    first_identity = embedding_identity(first)
    second_identity = embedding_identity(second)

    assert first_identity != second_identity
    assert "base_url" not in first_identity


def test_malformed_stored_identity_falls_back_to_rebuild_required_profile() -> None:
    from sag_api.sag.octx_vector_protocol import vector_profile_from_identity

    profile = vector_profile_from_identity(
        "chunk.content",
        {
            "model": "legacy/embedding",
            "model_fingerprint": "legacy-fingerprint",
            "dimensions": "not-a-number",
            "dtype": "float32",
            "normalized": False,
        },
        3,
    )

    assert profile["reuse_policy"] == "rebuild_required"
    assert "model" not in profile


async def test_prepare_vector_reuse_skips_rebuild_required_profiles(tmp_path: Path) -> None:
    from sag_api.sag.octx_importer import build_structured_plan
    from sag_api.sag.octx_vector_rebuilder import _vectors_for_role

    package = _transport_only_vector_package(tmp_path)
    plan_path = tmp_path / "transport-only-plan.sqlite3"
    build_structured_plan(package, plan_path, str(uuid.uuid4()))

    class Embedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 3

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.4, 0.5, 0.6] for _ in texts]

    embedding = Embedding()
    assert prepare_vector_reuse(package, plan_path, embedding) == set()
    generated = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": "019c2222-2222-7222-8222-222222222222"}})],
        ["Head\n\nBody"],
        embedding,
        plan_path=plan_path,
    )

    assert generated == [[0.4, 0.5, 0.6]]
    assert embedding.calls == [["Head\n\nBody"]]


def _compatible_reuse_package(
    tmp_path: Path,
    *,
    name: str,
    profiles: list[dict],
    arrow_rows: dict[str, list[tuple[str, list[float]]]],
    dimensions: int = 3,
    chunk_count: int = 1,
) -> Path:
    """Build a sag-structured package whose declared vector profiles are *compatible*.

    Structured data contains `chunk_count` chunks, one event, one entity and one
    event-entity relation; only the first chunk relates to the event. Arrow rows
    are written with input_sha256 values computed from the same records so reuse
    indexing accepts them.
    """
    import pyarrow as pa
    import pyarrow.ipc as ipc

    from sag_api.sag.octx_vector_protocol import ROLE_RECIPES, ROLE_TARGETS, render_recipe_input

    document_id = "019c1234-5678-7abc-8def-0123456789ab"
    chunk_ids = ["019c2222-2222-7222-8222-222222222222", "019c3333-3333-7333-8333-333333333333"][:chunk_count]
    event_id = "019c4444-4444-7444-8444-444444444444"
    entity_id = "019c5555-5555-7555-8555-555555555555"
    source = tmp_path / f"{name}-source"
    source.mkdir()
    (source / "guide.md").write_text(
        f"---\ntype: Reference\ntitle: Guide\noctx:\n  document_id: {document_id}\n---\n# Guide\n",
        encoding="utf-8",
    )
    workspace = tmp_path / f"{name}-workspace"
    create_octx(workspace, source=source, name=name, output=tmp_path / f"{name}-base.octx")
    chunk_records = {
        chunk_id: {
            "id": chunk_id,
            "document_id": document_id,
            "ordinal": index,
            "heading": f"Head{index or ''}",
            "text": f"Body{index or ''}",
        }
        for index, chunk_id in enumerate(chunk_ids)
    }
    _write_jsonl(workspace / "data/chunks.jsonl", list(chunk_records.values()))
    _write_jsonl(
        workspace / "data/events.jsonl",
        [{"id": event_id, "title": "Event", "content": "Event body"}],
    )
    _write_jsonl(workspace / "data/entities.jsonl", [{"id": entity_id, "name": "Entity", "type": "term"}])
    _write_jsonl(workspace / "relations/chunk-events.jsonl", [{"chunk_id": chunk_ids[0], "event_id": event_id}])
    _write_jsonl(
        workspace / "relations/event-entities.jsonl",
        [{"event_id": event_id, "entity_id": entity_id}],
    )

    def record_for(role: str, record_id: str) -> dict:
        if role.startswith("chunk."):
            return chunk_records[record_id]
        if role.startswith("event."):
            return {"id": event_id, "title": "Event", "content": "Event body"}
        if role == "entity.name":
            return {"id": entity_id, "name": "Entity", "type": "term"}
        return {
            "event_id": event_id,
            "entity_id": entity_id,
            "event": {"id": event_id, "title": "Event", "content": "Event body"},
            "entity": {"id": entity_id, "name": "Entity", "type": "term"},
        }

    vectors = workspace / "vectors"
    vectors.mkdir()
    (vectors / "profiles.json").write_text(json.dumps({"profiles": profiles}) + "\n", encoding="utf-8")
    for role, rows in arrow_rows.items():
        target = ROLE_TARGETS[role]
        recipe = ROLE_RECIPES[role]
        hashes = [
            input_sha256(render_recipe_input(recipe, record_for(role, record_id)))
            for record_id, _vector in rows
        ]
        schema = pa.schema(
            [
                pa.field("record_id", pa.string(), nullable=False),
                pa.field("input_sha256", pa.string(), nullable=False),
                pa.field("vector", pa.list_(pa.float32(), dimensions), nullable=False),
            ]
        )
        table = pa.Table.from_arrays(
            [
                pa.array([record_id for record_id, _vector in rows]),
                pa.array(hashes),
                pa.array([vector for _record_id, vector in rows], type=pa.list_(pa.float32(), dimensions)),
            ],
            schema=schema,
        )
        with pa.OSFile(str(vectors / f"{target}.arrow"), "wb") as output:
            with ipc.new_file(output, schema) as writer:
                writer.write_table(table)
    return create_octx(
        workspace,
        version="1.1.0",
        output=tmp_path / f"{name}.octx",
        capabilities={"sag-structured": "0.1", "vectors": "0.1"},
    ).output


def _compatible_identity(dimensions: int) -> dict:
    from sag_api.sag.octx_vector_protocol import embedding_identity

    return embedding_identity(
        SimpleNamespace(
            model="test/embedding",
            base_url="https://embedding.invalid/v1",
            dimensions=dimensions,
        )
    )


@pytest.mark.parametrize(
    ("configured_dimensions", "generated_vector"),
    [(None, [0.4, 0.5, 0.6]), (2, [0.4, 0.5])],
)
async def test_prepare_vector_reuse_rebuilds_role_when_dimensions_are_unknown_or_mismatched(
    tmp_path: Path,
    configured_dimensions: int | None,
    generated_vector: list[float],
) -> None:
    from sag_api.sag.octx_importer import build_structured_plan
    from sag_api.sag.octx_vector_protocol import vector_profile_from_identity

    chunk_id = "019c2222-2222-7222-8222-222222222222"
    profile = vector_profile_from_identity("chunk.content", _compatible_identity(3), 3)
    package = _compatible_reuse_package(
        tmp_path,
        name="dim-mismatch",
        profiles=[profile],
        arrow_rows={"chunk.content": [(chunk_id, [0.1, 0.2, 0.3])]},
    )
    assert validate_octx(package).capabilities["vectors"].valid
    plan_path = tmp_path / "dim-mismatch-plan.sqlite3"
    build_structured_plan(package, plan_path, str(uuid.uuid4()))

    class Embedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = configured_dimensions

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [generated_vector for _ in texts]

    embedding = Embedding()
    # An unknown or mismatched dimension must disable reuse without crashing the import.
    assert prepare_vector_reuse(package, plan_path, embedding) == set()

    from sag_api.sag.octx_vector_rebuilder import _vectors_for_role

    regenerated = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": chunk_id}})],
        ["Head\n\nBody"],
        embedding,
        plan_path=plan_path,
    )
    assert regenerated == [generated_vector]
    assert embedding.calls == [["Head\n\nBody"]]


async def test_partial_coverage_role_reuses_available_rows_and_rebuilds_missing(tmp_path: Path) -> None:
    from octx import vector_profile_fingerprint

    from sag_api.sag.octx_importer import build_structured_plan
    from sag_api.sag.octx_vector_protocol import vector_profile_from_identity

    chunk_id = "019c2222-2222-7222-8222-222222222222"
    chunk2_id = "019c3333-3333-7333-8333-333333333333"
    heading_profile = vector_profile_from_identity("chunk.heading", _compatible_identity(3), 3)
    content_profile = vector_profile_from_identity("chunk.content", _compatible_identity(3), 3)
    content_profile["coverage"] = "partial"
    content_profile["fingerprint"] = vector_profile_fingerprint(content_profile)
    package = _compatible_reuse_package(
        tmp_path,
        name="partial-coverage",
        profiles=[heading_profile, content_profile],
        arrow_rows={
            "chunk.heading": [(chunk_id, [0.1, 0.2, 0.3]), (chunk2_id, [0.2, 0.3, 0.4])],
            "chunk.content": [(chunk_id, [0.1, 0.2, 0.3])],
        },
        chunk_count=2,
    )
    assert validate_octx(package).capabilities["vectors"].valid
    plan_path = tmp_path / "partial-coverage-plan.sqlite3"
    build_structured_plan(package, plan_path, str(uuid.uuid4()))

    class Embedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 3

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.4, 0.5, 0.6] for _ in texts]

    embedding = Embedding()
    # Compatible partial coverage should index the available rows so only the
    # missing records are regenerated.
    assert prepare_vector_reuse(package, plan_path, embedding) == {"chunk.heading", "chunk.content"}

    from sag_api.sag.octx_vector_rebuilder import _vectors_for_role

    records = [
        SimpleNamespace(extra_data={"octx": {"record_id": chunk_id}}),
        SimpleNamespace(extra_data={"octx": {"record_id": chunk2_id}}),
    ]
    reused_heading = await _vectors_for_role(
        "chunk.heading",
        records,
        ["Head", "Head2"],
        embedding,
        plan_path=plan_path,
    )
    assert reused_heading == [pytest.approx([0.1, 0.2, 0.3]), pytest.approx([0.2, 0.3, 0.4])]
    assert embedding.calls == []

    regenerated_content = await _vectors_for_role(
        "chunk.content",
        records,
        ["Head\n\nBody", "Head2\n\nBody2"],
        embedding,
        plan_path=plan_path,
    )
    assert regenerated_content == [pytest.approx([0.1, 0.2, 0.3]), pytest.approx([0.4, 0.5, 0.6])]
    assert embedding.calls == [["Head2\n\nBody2"]]


async def test_import_reuses_declared_roles_and_rebuilds_undeclared_roles(tmp_path: Path) -> None:
    from sag_api.sag.octx_importer import build_structured_plan, import_structured_plan
    from sag_api.sag.octx_vector_protocol import vector_profile_from_identity
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    chunk_id = "019c2222-2222-7222-8222-222222222222"
    identity = _compatible_identity(3)
    profiles = [
        vector_profile_from_identity("chunk.heading", identity, 3),
        vector_profile_from_identity("chunk.content", identity, 3),
    ]
    package = _compatible_reuse_package(
        tmp_path,
        name="partial-roles",
        profiles=profiles,
        arrow_rows={
            "chunk.heading": [(chunk_id, [0.1, 0.2, 0.3])],
            "chunk.content": [(chunk_id, [0.1, 0.2, 0.3])],
        },
    )
    assert validate_octx(package).capabilities["vectors"].valid

    class TrackingEmbedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 3

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.4, 0.5, 0.6] for _ in texts]

    embedding = TrackingEmbedding()
    plan_path = tmp_path / "partial-roles-plan.sqlite3"
    namespace = str(uuid.uuid4())
    build_structured_plan(package, plan_path, namespace)
    assert prepare_vector_reuse(package, plan_path, embedding) == {"chunk.heading", "chunk.content"}

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.schema import create_missing_relation_tables

    class VectorStore:
        def __init__(self) -> None:
            self.collections: list[str] = []
            self.records: dict[str, list[Any]] = {}

        async def upsert(self, collection, records):
            from zleap.sag.core.adapters.models import BulkResult

            assert all(record.payload["data_source_id"] == "shadow-partial-roles" for record in records)
            self.collections.append(collection)
            self.records.setdefault(collection, []).extend(records)
            return BulkResult(succeeded_ids=tuple(record.id for record in records))

        async def publish(self, collections):
            self.published = tuple(collections)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial-roles-sag.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await create_missing_relation_tables(engine, "normal")
    await import_structured_plan(
        plan_path,
        namespace,
        source_config_id="shadow-partial-roles",
        source_name="Shadow",
        session_factory=sessions,
    )
    store = VectorStore()
    try:
        stats = await rebuild_vectors(
            "shadow-partial-roles",
            {},
            session_factory=sessions,
            embedding_client=embedding,
            vector_store=store,
            package_path=package,
            plan_path=plan_path,
        )
    finally:
        await engine.dispose()

    assert stats == {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}
    assert store.collections == [
        "source_chunks",
        "event_vectors_wide",
        "entity_vectors",
        "event_entity_vectors",
    ]
    # Declared chunk roles are reused: no embedding call and stored vectors
    # match the package payload.
    chunk_vectors = store.records["source_chunks"][0].vectors
    assert chunk_vectors["heading_vector"] == pytest.approx([0.1, 0.2, 0.3])
    assert chunk_vectors["content_vector"] == pytest.approx([0.1, 0.2, 0.3])
    # Undeclared roles are rebuilt through the embedding provider.
    assert embedding.calls == [
        ["Event"],
        ["Event\n\nEvent body"],
        ["Entity"],
        ["Event\n\nEntity"],
    ]


def test_export_profile_is_rebuild_required_when_stored_dimensions_disagree() -> None:
    from sag_api.sag.octx_vector_protocol import vector_profile_from_identity

    # The stored identity claims 3 dimensions but the role really holds 2:
    # the identity is no longer provable, so the profile must degrade to
    # rebuild_required instead of claiming compatibility.
    profile = vector_profile_from_identity("chunk.content", _compatible_identity(3), 2)

    assert profile["reuse_policy"] == "rebuild_required"
    assert "model" not in profile
    assert "fingerprint" not in profile


async def test_rebuild_vectors_falls_back_to_generation_when_reuse_preparation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zleap.sag.db.models import DataSource, SourceChunk

    from sag_api.sag import octx_vector_reuse
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    def broken_prepare(*_args, **_kwargs):
        raise OSError("temporary vector payload disappeared")

    monkeypatch.setattr(octx_vector_reuse, "prepare_vector_reuse", broken_prepare)

    class TrackingEmbedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 3

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.4, 0.5, 0.6] for _ in texts]

    class VectorStore:
        def __init__(self) -> None:
            self.collections: list[str] = []

        async def upsert(self, collection, records):
            from zleap.sag.core.adapters.models import BulkResult

            self.collections.append(collection)
            return BulkResult(succeeded_ids=tuple(record.id for record in records))

        async def publish(self, collections):
            self.published = tuple(collections)

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.schema import create_missing_relation_tables

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fallback-sag.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await create_missing_relation_tables(engine, "normal")
    async with sessions() as session:
        session.add(DataSource(id="src-fallback", name="Fallback"))
        session.add(
            SourceChunk(
                id="019c2222-2222-7222-8222-222222222222",
                data_source_id="src-fallback",
                source_type="article",
                source_id="article-fallback",
                heading="Head",
                content="Body",
                rank=0,
                chunk_length=8,
            )
        )
        await session.commit()

    embedding = TrackingEmbedding()
    store = VectorStore()
    checkpoint: dict = {}
    try:
        stats = await rebuild_vectors(
            "src-fallback",
            checkpoint,
            session_factory=sessions,
            embedding_client=embedding,
            vector_store=store,
            package_path=tmp_path / "unused.octx",
            plan_path=tmp_path / "unused-plan.sqlite3",
        )
    finally:
        await engine.dispose()

    assert stats == {"chunks": 1, "events": 0, "entities": 0, "event_entities": 0}
    assert store.collections == ["source_chunks"]
    assert checkpoint["reusable_roles"] == []
    # The failure degraded to a full generation path instead of crashing.
    assert embedding.calls == [["Head\n\nBody"], ["Head"]]


def test_vector_arrow_conversion_reuses_float_lists_without_flattening_them() -> None:
    import pyarrow as pa

    from sag_api.sag.octx_vector_protocol import (
        _float_vector,
        _normalize_arrow_vector_column,
        _vector_array,
    )

    stored = [0.1, 0.2, 0.3]

    assert _float_vector(stored) is stored
    array = _vector_array([stored, [0.4, 0.5, 0.6]], dimension=3)
    assert array.to_pylist() == [
        pytest.approx([0.1, 0.2, 0.3]),
        pytest.approx([0.4, 0.5, 0.6]),
    ]
    variable = pa.array([[0.1, 0.2], [0.3, 0.4]], type=pa.list_(pa.float32()))
    normalized = _normalize_arrow_vector_column(variable)
    assert pa.types.is_fixed_size_list(normalized.type)
    assert normalized.type.list_size == 2
    assert normalized.to_pylist() == variable.to_pylist()


def test_reused_vector_batch_size_stays_inside_memory_budget() -> None:
    from sag_api.sag.octx_vector_rebuilder import _effective_reuse_batch_size

    assert _effective_reuse_batch_size(500, dimensions=1024, role_count=1) == 500
    assert _effective_reuse_batch_size(2000, dimensions=4096, role_count=2) == 512
    assert _effective_reuse_batch_size(40, dimensions=1024, role_count=1) == 40


def test_octx_reuse_optimization_has_safe_defaults() -> None:
    from sag_api.core.config import Settings

    configured = Settings(_env_file=None)

    assert configured.octx_arrow_vector_reuse_enabled is True
    assert configured.octx_reused_vector_batch_size == 500
    assert configured.octx_vector_progress_interval_seconds == 1.0


async def test_lancedb_arrow_stream_exports_large_role_without_python_vector_materialization(tmp_path: Path) -> None:
    import re

    import pyarrow as pa
    import pyarrow.ipc as ipc

    row_count = 5001
    workspace = tmp_path / "workspace"
    records = [
        {
            "id": f"chunk-{index:05d}",
            "document_id": "doc-1",
            "ordinal": index,
            "heading": f"Heading {index}",
            "text": f"Body {index}",
        }
        for index in range(row_count)
    ]
    _write_jsonl(workspace / "data/chunks.jsonl", records)
    _write_jsonl(workspace / "data/events.jsonl", [])
    _write_jsonl(workspace / "data/entities.jsonl", [])
    _write_jsonl(workspace / "relations/event-entities.jsonl", [])

    vectors = {f"local-{index:05d}": [float(index), float(index + 1)] for index in range(row_count)}
    streamed_ids = [*vectors, "local-00000", "local-extra"]
    vectors["local-extra"] = [-1.0, -1.0]

    class Query:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def where(self, predicate: str):
            self.ids = list(streamed_ids) if "source_config_id" in predicate else re.findall(r"'([^']+)'", predicate)
            return self

        def select(self, _fields: list[str]):
            return self

        def order_by(self, _ordering):
            self.ids.sort()
            return self

        async def to_batches(self, *, max_batch_length: int):
            assert max_batch_length <= 500
            ids = list(reversed(self.ids)) if len(self.ids) <= 500 else self.ids
            schema = pa.schema(
                [
                    pa.field("id", pa.string()),
                    pa.field("heading_vector", pa.list_(pa.float32(), 2)),
                ]
            )
            batch = pa.RecordBatch.from_arrays(
                [
                    pa.array(ids, type=pa.string()),
                    pa.array([vectors[record_id] for record_id in ids], type=pa.list_(pa.float32(), 2)),
                ],
                schema=schema,
            )
            class AsyncReader:
                def __aiter__(self):
                    async def batches():
                        for offset in range(0, len(ids), max_batch_length):
                            yield batch.slice(offset, max_batch_length)

                    return batches()

            return AsyncReader()

        async def to_list(self):
            raise AssertionError("Arrow-native export must not materialize vectors as Python lists")

    class Table:
        query_count = 0

        def query(self):
            self.query_count += 1
            return Query()

    table = Table()

    class LanceStore:
        async def _open_table(self, _index: str):
            return table

    LanceStore.__module__ = "zleap.sag.core.storage.lancedb_store"

    class Embedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 2

    from sag_api.sag.octx_vector_manifest import VectorExportManifest

    manifest_path = tmp_path / "vector-export.sqlite3"
    with VectorExportManifest(manifest_path) as manifest:
        for index, record in enumerate(records):
            manifest.add(
                "chunk.heading",
                f"local-{index:05d}",
                record["id"],
                input_sha256(render_role_input("chunk.heading", record)),
            )

    roles = await write_existing_vector_payload(
        workspace,
        LanceStore(),
        Embedding(),
        manifest_path=manifest_path,
        routing="source-config-1",
        batch_size=500,
    )

    assert roles == {"chunk.heading"}
    assert table.query_count == 1
    with pa.memory_map(str(workspace / "vectors/chunk_heading.arrow"), "r") as source:
        table = ipc.open_file(source).read_all()
    assert table.num_rows == row_count
    assert table.column("record_id").to_pylist()[:2] == ["chunk-00000", "chunk-00001"]
    assert table.column("vector")[0].as_py() == pytest.approx([0.0, 1.0])
    assert table.column("vector")[-1].as_py() == pytest.approx([5000.0, 5001.0])


async def test_write_vector_payload_creates_valid_vectors_v01_package(tmp_path: Path) -> None:
    vector_a = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    vector_b = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]
    doc_id = "019c1234-5678-7abc-8def-0123456789ab"
    chunk_id = "019c2222-2222-7222-8222-222222222222"
    event_id = "019c4444-4444-7444-8444-444444444444"
    entity_id = "019c5555-5555-7555-8555-555555555555"
    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.md").write_text(
        f"---\ntype: Reference\ntitle: Guide\noctx:\n  document_id: {doc_id}\n---\n# Guide\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    create_octx(workspace, source=source, name="SAG vectors", output=tmp_path / "base.octx")
    _write_jsonl(
        workspace / "data/chunks.jsonl",
        [{"id": chunk_id, "document_id": doc_id, "ordinal": 0, "heading": "Head", "text": "Body"}],
    )
    _write_jsonl(
        workspace / "data/events.jsonl",
        [{"id": event_id, "title": "Event", "content": "Event body"}],
    )
    _write_jsonl(workspace / "data/entities.jsonl", [{"id": entity_id, "name": "Entity", "type": "term"}])
    _write_jsonl(workspace / "relations/chunk-events.jsonl", [{"chunk_id": chunk_id, "event_id": event_id}])
    _write_jsonl(
        workspace / "relations/event-entities.jsonl",
        [{"event_id": event_id, "entity_id": entity_id}],
    )

    class Embedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 9
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [vector_a for _ in texts]

    relation_key = f"event_entity:{event_id}:{entity_id}"
    source_ids = {
        "chunk.heading": {chunk_id: chunk_id},
        "chunk.content": {chunk_id: chunk_id},
        "event.title": {event_id: event_id},
        "event.content": {event_id: event_id},
        "entity.name": {entity_id: entity_id},
        "event_entity.relation": {relation_key: relation_key},
    }

    class ExistingVectors:
        async def fetch_vector_fields(self, index, ids, fields):
            return {record_id: {field: vector_a for field in fields} for record_id in ids}

    roles = await write_existing_vector_payload(
        workspace,
        ExistingVectors(),
        Embedding(),
        source_ids=source_ids,
        batch_size=2,
    )
    package = create_octx(
        workspace,
        version="1.1.0",
        output=tmp_path / "vectors.octx",
        capabilities={"sag-structured": "0.1", "vectors": "0.1"},
    ).output

    assert roles == {
        "chunk.heading",
        "chunk.content",
        "event.title",
        "event.content",
        "entity.name",
        "event_entity.relation",
    }
    report = validate_octx(package)
    assert report.capabilities["vectors"].valid
    with open_octx(package) as opened:
        relation = opened.read_vector_table("event_entity_relation").to_pylist()[0]
    assert relation["record_id"] == f"event_entity:{event_id}:{entity_id}"

    from sag_api.sag.octx_importer import build_structured_plan

    plan_path = tmp_path / "plan.sqlite3"
    namespace = str(uuid.uuid4())
    build_structured_plan(package, plan_path, namespace)
    assert (
        prepare_vector_reuse(
            package,
            plan_path,
            Embedding(),
            prevalidated_vector_valid=False,
        )
        == set()
    )
    reusable = prepare_vector_reuse(package, plan_path, Embedding())

    assert reusable == roles
    import sqlite3

    with sqlite3.connect(plan_path) as connection:
        reuse_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(vector_reuse_locations)")
        }
        assert reuse_columns == {
            "role",
            "record_id",
            "batch_index",
            "row_index",
            "input_sha256",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM vector_reuse_locations"
        ).fetchone() == (6,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vector_reuse'"
        ).fetchone() is None

    with ArrowVectorReuseReader(plan_path) as reuse_reader:
        assert reuse_reader.get_many("chunk.content", [chunk_id]) == {
            chunk_id: pytest.approx(vector_a)
        }
    assert get_reused_vector(plan_path, "chunk.content", chunk_id) == pytest.approx(vector_a)

    from sag_api.sag.octx_vector_rebuilder import _vectors_for_role

    class NoEmbedding:
        async def batch_generate(self, _texts):
            raise AssertionError("compatible OCTX vector must skip Embedding")

    reused = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": chunk_id}})],
        ["Head\n\nBody"],
        NoEmbedding(),
        reuse_reader=ArrowVectorReuseReader(plan_path),
    )
    assert len(reused) == 1
    assert reused[0] == pytest.approx(vector_a)

    class BatchReader:
        calls: list[tuple[str, list[str]]] = []

        def get_many(self, role: str, record_ids: list[str]):
            self.calls.append((role, list(record_ids)))
            return {
                "chunk-a": vector_a,
                "chunk-b": vector_b,
            }

    batch_reader = BatchReader()
    batch_reused = await _vectors_for_role(
        "chunk.content",
        [
            SimpleNamespace(extra_data={"octx": {"record_id": "chunk-a"}}),
            SimpleNamespace(extra_data={"octx": {"record_id": "chunk-b"}}),
        ],
        ["A", "B"],
        NoEmbedding(),
        reuse_reader=batch_reader,
    )

    assert batch_reader.calls == [("chunk.content", ["chunk-a", "chunk-b"])]
    assert batch_reused == [
        pytest.approx(vector_a),
        pytest.approx(vector_b),
    ]

    class BrokenReader:
        def get_many(self, _role: str, _record_ids: list[str]):
            raise OSError("temporary Arrow payload disappeared")

    class FallbackEmbedding:
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [vector_b for _ in texts]

    fallback_embedding = FallbackEmbedding()
    fallback = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": "chunk-a"}})],
        ["A"],
        fallback_embedding,
        reuse_reader=BrokenReader(),
    )

    assert fallback == [vector_b]
    assert fallback_embedding.calls == [["A"]]

    from zleap.sag import DataEngine
    from zleap.sag.core.adapters.models import Filter, VectorQuery

    from sag_api.core.config import Settings
    from sag_api.sag.config_builder import build_engine_config
    from sag_api.sag.octx_importer import import_structured_plan
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    target_dir = tmp_path / "target-engine"
    settings = Settings(
        data_dir=str(target_dir),
        llm_api_key="fixture",
        embedding_api_key="fixture",
        embedding_dimensions=9,
        _env_file=None,
    )
    data_engine = DataEngine(build_engine_config(settings), health_check=False)
    await data_engine.start()
    sessions = data_engine.resources.relational.session_factory()

    class CompatibleNoEmbedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 9

        async def batch_generate(self, _texts):
            raise AssertionError("full compatible package must skip all Embedding calls")

    try:
        await import_structured_plan(
            plan_path,
            namespace,
            source_config_id="shadow-vectors-v02",
            source_name="Shadow",
            session_factory=sessions,
        )
        stats = await rebuild_vectors(
            "shadow-vectors-v02",
            {},
            session_factory=sessions,
            embedding_client=CompatibleNoEmbedding(),
            vector_store=data_engine.resources.vector,
            package_path=package,
            plan_path=plan_path,
        )
        names = await data_engine.resources.vector.schema_object_names()
        hits = await data_engine.resources.vector.query(
            "source_chunks",
            VectorQuery(
                vector=vector_a,
                vector_field="content_vector",
                filters=Filter.eq("data_source_id", "shadow-vectors-v02"),
                limit=1,
            ),
        )
    finally:
        await data_engine.aclose()

    assert stats == {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}
    assert names >= {
        "source_chunks",
        "event_vectors_wide",
        "entity_vectors",
        "event_entity_vectors",
    }
    assert hits and hits[0].payload["data_source_id"] == "shadow-vectors-v02"

    existing_workspace = tmp_path / "existing-workspace"
    shutil.copytree(workspace, existing_workspace)
    shutil.rmtree(existing_workspace / "vectors")
    source_ids = {
        "chunk.heading": {chunk_id: "local-chunk"},
        "chunk.content": {chunk_id: "local-chunk"},
        "event.title": {event_id: "local-event"},
        "event.content": {event_id: "local-event"},
        "entity.name": {entity_id: "local-entity"},
        "event_entity.relation": {relation_key: "local-relation"},
    }

    existing_roles = await write_existing_vector_payload(
        existing_workspace,
        ExistingVectors(),
        Embedding(),
        source_ids=source_ids,
    )
    existing_package = create_octx(
        existing_workspace,
        version="1.2.0",
        output=tmp_path / "existing-vectors.octx",
        capabilities={"sag-structured": "0.1", "vectors": "0.1"},
    ).output

    assert existing_roles == roles
    assert validate_octx(existing_package).capabilities["vectors"].valid

    unavailable_workspace = tmp_path / "unavailable-workspace"
    shutil.copytree(workspace, unavailable_workspace)
    shutil.rmtree(unavailable_workspace / "vectors")

    class UnavailableVectors:
        async def fetch_vector_fields(self, index, ids, fields):
            raise ConnectionError("vector store unavailable")

    unavailable_roles = await write_existing_vector_payload(
        unavailable_workspace,
        UnavailableVectors(),
        Embedding(),
        source_ids=source_ids,
    )

    assert unavailable_roles == set()
    assert not (unavailable_workspace / "vectors").exists()

    mismatched_plan = tmp_path / "mismatched-plan.sqlite3"
    build_structured_plan(package, mismatched_plan, namespace)

    class DifferentEmbedding(Embedding):
        model = "different/embedding"
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.4, 0.5, 0.6] for _ in texts]

    different = DifferentEmbedding()
    assert prepare_vector_reuse(package, mismatched_plan, different) == set()
    regenerated = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": chunk_id}})],
        ["Head\n\nBody"],
        different,
        plan_path=mismatched_plan,
    )
    assert regenerated == [[0.4, 0.5, 0.6]]
    assert different.calls == [["Head\n\nBody"]]

    tampered_workspace = tmp_path / "tampered-workspace"
    shutil.copytree(workspace, tampered_workspace)
    heading_path = tampered_workspace / "vectors/chunk_heading.arrow"
    import pyarrow as pa
    import pyarrow.ipc as ipc

    with pa.memory_map(str(heading_path), "r") as source_file:
        heading_table = ipc.open_file(source_file).read_all()
    heading_table = heading_table.set_column(
        heading_table.schema.get_field_index("input_sha256"),
        heading_table.schema.field("input_sha256"),
        pa.array(["0" * 64] * heading_table.num_rows, type=pa.string()),
    )
    replacement_path = heading_path.with_suffix(".arrow.tmp")
    with pa.OSFile(str(replacement_path), "wb") as output_file:
        with ipc.new_file(output_file, heading_table.schema) as writer:
            writer.write_table(heading_table)
    replacement_path.replace(heading_path)
    tampered_package = create_octx(
        tampered_workspace,
        version="1.3.0",
        output=tmp_path / "tampered-input-hash.octx",
        capabilities={"sag-structured": "0.1", "vectors": "0.1"},
    ).output
    tampered_plan = tmp_path / "tampered-plan.sqlite3"
    build_structured_plan(tampered_package, tampered_plan, namespace)

    hash_checked_roles = prepare_vector_reuse(tampered_package, tampered_plan, Embedding())

    assert "chunk.heading" not in hash_checked_roles
    assert hash_checked_roles == roles - {"chunk.heading"}
