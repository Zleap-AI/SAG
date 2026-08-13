from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

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
        dimensions = 3
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.1, 0.2, 0.3] for _ in texts]

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
            return {record_id: {field: [0.1, 0.2, 0.3] for field in fields} for record_id in ids}

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
            chunk_id: pytest.approx([0.1, 0.2, 0.3])
        }
    assert get_reused_vector(plan_path, "chunk.content", chunk_id) == pytest.approx([0.1, 0.2, 0.3])

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
    assert reused[0] == pytest.approx([0.1, 0.2, 0.3])

    class BatchReader:
        calls: list[tuple[str, list[str]]] = []

        def get_many(self, role: str, record_ids: list[str]):
            self.calls.append((role, list(record_ids)))
            return {
                "chunk-a": [0.1, 0.2, 0.3],
                "chunk-b": [0.4, 0.5, 0.6],
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
        pytest.approx([0.1, 0.2, 0.3]),
        pytest.approx([0.4, 0.5, 0.6]),
    ]

    class BrokenReader:
        def get_many(self, _role: str, _record_ids: list[str]):
            raise OSError("temporary Arrow payload disappeared")

    class FallbackEmbedding:
        calls: list[list[str]] = []

        async def batch_generate(self, texts):
            self.calls.append(list(texts))
            return [[0.7, 0.8, 0.9] for _ in texts]

    fallback_embedding = FallbackEmbedding()
    fallback = await _vectors_for_role(
        "chunk.content",
        [SimpleNamespace(extra_data={"octx": {"record_id": "chunk-a"}})],
        ["A"],
        fallback_embedding,
        reuse_reader=BrokenReader(),
    )

    assert fallback == [[0.7, 0.8, 0.9]]
    assert fallback_embedding.calls == [["A"]]

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from zleap.sag.db.base import Base

    from sag_api.sag.octx_importer import import_structured_plan
    from sag_api.sag.octx_vector_rebuilder import rebuild_vectors

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sag.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await import_structured_plan(
        plan_path,
        namespace,
        source_config_id="shadow-vectors-v02",
        source_name="Shadow",
        session_factory=sessions,
    )

    class CompatibleNoEmbedding:
        model = "test/embedding"
        base_url = "https://embedding.invalid/v1"
        dimensions = 3

        async def batch_generate(self, _texts):
            raise AssertionError("full compatible package must skip all Embedding calls")

    class VectorStore:
        indexes: list[str] = []

        async def bulk_index(self, *, index, documents, return_details, routing):
            assert routing == "shadow-vectors-v02"
            self.indexes.append(index)
            return {"success_count": len(documents), "error_count": 0}

    store = VectorStore()
    try:
        stats = await rebuild_vectors(
            "shadow-vectors-v02",
            {},
            session_factory=sessions,
            embedding_client=CompatibleNoEmbedding(),
            vector_store=store,
            package_path=package,
            plan_path=plan_path,
        )
    finally:
        await engine.dispose()

    assert stats == {"chunks": 1, "events": 1, "entities": 1, "event_entities": 1}
    assert store.indexes == ["source_chunks", "event_vectors", "entity_vectors", "event_entity_vectors"]

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
