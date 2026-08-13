from __future__ import annotations

import gc
import hashlib
import json
import logging
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from octx import vector_profile_fingerprint

logger = logging.getLogger(__name__)

ROLE_TARGETS = {
    "chunk.heading": "chunk_heading",
    "chunk.content": "chunk_content",
    "event.title": "event_title",
    "event.content": "event_content",
    "entity.name": "entity_name",
    "event_entity.relation": "event_entity_relation",
}

ROLE_RECIPES: dict[str, dict[str, Any]] = {
    "chunk.heading": {
        "fields": ["heading"],
        "separator": "",
        "fallback": {"fields": ["text"], "separator": ""},
    },
    "chunk.content": {"fields": ["heading", "text"], "separator": "\n\n"},
    "event.title": {"fields": ["title"], "separator": ""},
    "event.content": {"fields": ["title", "content"], "separator": "\n\n"},
    "entity.name": {"fields": ["name"], "separator": ""},
    "event_entity.relation": {
        "fields": ["description"],
        "separator": "",
        "fallback": {"fields": ["event.title", "entity.name"], "separator": "\n\n"},
    },
}

ROLE_STORAGE = {
    "chunk.heading": ("source_chunks", "heading_vector"),
    "chunk.content": ("source_chunks", "content_vector"),
    "event.title": ("event_vectors", "title_vector"),
    "event.content": ("event_vectors", "content_vector"),
    "entity.name": ("entity_vectors", "vector"),
    "event_entity.relation": ("event_entity_vectors", "vector"),
}

RECIPE_IDS = {role: f"sag.{role.replace('.', '-')}/1" for role in ROLE_TARGETS}
_CANONICALIZATION = {
    "encoding": "UTF-8",
    "unicode_normalization": "NFC",
    "newline": "LF",
    "null_as": "",
    "trim": False,
}


def _canonical_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def _field(record: dict[str, Any], path: str) -> object:
    value: object = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def render_role_input(role: str, record: dict[str, Any]) -> str:
    return render_recipe_input(ROLE_RECIPES[role], record)


def render_recipe_input(recipe: dict[str, Any], record: dict[str, Any]) -> str:
    """Render one vector input from the declarative recipe carried by OCTX."""

    def render(group: dict[str, Any]) -> str:
        return str(group["separator"]).join(_canonical_text(_field(record, name)) for name in group["fields"])

    rendered = render(recipe)
    fallback = recipe.get("fallback")
    return render(fallback) if rendered == "" and isinstance(fallback, dict) else rendered


def input_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_fingerprint(embedding_client: Any) -> str:
    identity = {
        "implementation": "openai-compatible/1",
        "model": str(getattr(embedding_client, "model", "")),
        # The endpoint is hashed, never serialized. This prevents two providers
        # from reusing vectors merely because they expose the same mutable alias.
        "base_url": str(getattr(embedding_client, "base_url", "") or ""),
        "dimensions": getattr(embedding_client, "dimensions", None),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "sag-config:sha256:" + hashlib.sha256(encoded).hexdigest()


def embedding_identity(embedding_client: Any) -> dict[str, Any] | None:
    dimensions = getattr(embedding_client, "dimensions", None)
    model = str(getattr(embedding_client, "model", "")).strip()
    if not model or dimensions is None:
        return None
    return {
        "provider": "openai-compatible",
        "model": model,
        "model_fingerprint": model_fingerprint(embedding_client),
        "dimensions": int(dimensions),
        "dtype": "float32",
        "normalized": False,
    }


def configured_embedding_identity(settings: Any) -> dict[str, Any] | None:
    class Configuration:
        model = settings.embedding_model
        base_url = settings.effective_embedding_base_url
        dimensions = settings.embedding_dimensions

    return embedding_identity(Configuration())


def vector_profile(role: str, embedding_client: Any, dimensions: int) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "role": role,
        "provider": "openai-compatible",
        "model": str(getattr(embedding_client, "model", "")),
        "model_fingerprint": model_fingerprint(embedding_client),
        "dimensions": dimensions,
        "dtype": "float32",
        "normalized": False,
        "coverage": "complete",
        "recipe_id": RECIPE_IDS[role],
        "recipe": {**ROLE_RECIPES[role], **_CANONICALIZATION},
        "input_hash_algorithm": "sha256",
    }
    profile["fingerprint"] = vector_profile_fingerprint(profile)
    return profile


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [dict(json.loads(line)) for line in handle if line.strip()]


def _float_vector(value: Any) -> list[float]:
    if isinstance(value, list) and all(isinstance(item, float) for item in value):
        return value
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, (list, tuple)) and hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)):
        raise TypeError("stored vector is not an array")
    return [float(item) for item in value]


def _vector_array(values: list[list[float]], *, dimension: int):
    import pyarrow as pa

    return pa.array(values, type=pa.list_(pa.float32(), dimension))


def _normalize_arrow_vector_column(vectors: Any):
    """Accept fixed or uniformly sized Arrow float32 vectors without Python materialization."""
    import pyarrow as pa
    import pyarrow.compute as pc

    if vectors.null_count:
        raise ValueError("LanceDB vector column contains null values")
    if pa.types.is_fixed_size_list(vectors.type):
        if not pa.types.is_float32(vectors.type.value_type):
            raise ValueError("LanceDB vector column must use float32")
        return vectors
    if not (pa.types.is_list(vectors.type) or pa.types.is_large_list(vectors.type)):
        raise ValueError("LanceDB vector column has an unsupported type")
    if not pa.types.is_float32(vectors.type.value_type):
        raise ValueError("LanceDB vector column must use float32")
    lengths = pc.list_value_length(vectors)
    minimum = pc.min(lengths).as_py()
    maximum = pc.max(lengths).as_py()
    if minimum is None or minimum < 1 or minimum != maximum:
        raise ValueError("LanceDB vector column has inconsistent dimensions")
    return pa.FixedSizeListArray.from_arrays(vectors.flatten(), int(minimum))


def _role_records(workspace: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    chunks = _read_jsonl(workspace / "data/chunks.jsonl")
    events = _read_jsonl(workspace / "data/events.jsonl")
    entities = _read_jsonl(workspace / "data/entities.jsonl")
    relations = _read_jsonl(workspace / "relations/event-entities.jsonl")
    events_by_id = {str(record["id"]): record for record in events}
    entities_by_id = {str(record["id"]): record for record in entities}
    relation_records = [
        (
            f"event_entity:{relation['event_id']}:{relation['entity_id']}",
            {
                **relation,
                "event": events_by_id[str(relation["event_id"])],
                "entity": entities_by_id[str(relation["entity_id"])],
            },
        )
        for relation in relations
    ]
    return {
        "chunk.heading": [(str(record["id"]), record) for record in chunks],
        "chunk.content": [(str(record["id"]), record) for record in chunks],
        "event.title": [(str(record["id"]), record) for record in events],
        "event.content": [(str(record["id"]), record) for record in events],
        "entity.name": [(str(record["id"]), record) for record in entities],
        "event_entity.relation": relation_records,
    }


async def _iter_lancedb_vector_batches(
    vector_store: Any,
    index: str,
    vector_field: str,
    *,
    role: str,
    routing: str | None,
    batch_size: int,
    manifest: Any = None,
    records_by_local_id: dict[str, tuple[str, dict[str, Any]]] | None = None,
):
    """Stream one LanceDB role in a single query without Python vector values."""
    import pyarrow as pa
    import pyarrow.compute as pc
    from lancedb.query import ColumnOrdering

    table = await vector_store._open_table(index)
    if table is None:
        return
    if routing:
        escaped_routing = routing.replace("'", "''")
        predicate = f"source_config_id = '{escaped_routing}'"
    else:
        if records_by_local_id is None:
            raise ValueError("LanceDB manifest export requires source routing")
        record_ids = records_by_local_id.keys()
        quoted = ",".join("'" + record_id.replace("'", "''") + "'" for record_id in record_ids)
        predicate = f"id IN ({quoted})"
    query = (
        table.query()
        .where(predicate)
        .select(["id", vector_field])
        .order_by([ColumnOrdering(column_name="id")])
    )
    reader = await query.to_batches(max_batch_length=batch_size)
    seen: set[str] = set()
    async for batch in reader:
        ids = [str(value) for value in batch.column("id").to_pylist()]
        if manifest is not None:
            selected = manifest.lookup(role, ids)
        else:
            assert records_by_local_id is not None
            selected = {
                local_id: (
                    records_by_local_id[local_id][0],
                    input_sha256(render_role_input(role, records_by_local_id[local_id][1])),
                )
                for local_id in ids
                if local_id in records_by_local_id
            }
        batch_seen: set[str] = set()
        positions: list[int] = []
        for position, record_id in enumerate(ids):
            if record_id not in selected or record_id in seen or record_id in batch_seen:
                continue
            batch_seen.add(record_id)
            positions.append(position)
        selected_ids = [ids[position] for position in positions]
        if not selected_ids:
            continue
        seen.update(selected_ids)
        vectors = _normalize_arrow_vector_column(batch.column(vector_field))
        if len(positions) != batch.num_rows:
            vectors = pc.take(vectors, pa.array(positions, type=pa.int32()))
        yield [selected[local_id] for local_id in selected_ids], vectors


async def _fetch_vector_fields(
    vector_store: Any,
    index: str,
    record_ids: list[str],
    fields: list[str],
    *,
    routing: str | None = None,
) -> dict[str, dict[str, Any]]:
    custom = getattr(vector_store, "fetch_vector_fields", None)
    if callable(custom):
        return dict(await custom(index, record_ids, fields))
    module = type(vector_store).__module__
    if module.endswith("lancedb_store"):
        table = await vector_store._open_table(index)
        if table is None:
            return {}
        quoted = ",".join("'" + record_id.replace("'", "''") + "'" for record_id in record_ids)
        rows = await table.query().where(f"id IN ({quoted})").select(["id", *fields]).to_list()
        return {str(row["id"]): dict(row) for row in rows}
    if module.endswith(("pgvector_store", "oceanbase_store")):
        from sqlalchemy import bindparam, text

        quote = '"' if module.endswith("pgvector_store") else "`"
        columns = ", ".join(f"{quote}{field}{quote}" for field in fields)
        statement = text(
            f"SELECT {quote}id{quote}, {columns} FROM {quote}{index}{quote} WHERE {quote}id{quote} IN :record_ids"
        ).bindparams(bindparam("record_ids", expanding=True))
        async with vector_store._engine().connect() as connection:
            result = await connection.execute(statement, {"record_ids": record_ids})
            return {str(row._mapping["id"]): dict(row._mapping) for row in result}
    client = getattr(vector_store, "client", None)
    mget = getattr(client, "mget", None)
    if callable(mget):
        documents = [
            {
                "_id": record_id,
                "_source": fields,
                **({"routing": routing} if routing else {}),
            }
            for record_id in record_ids
        ]
        response = await mget(index=index, docs=documents)
        return {
            str(item["_id"]): dict(item.get("_source") or {}) for item in response.get("docs", []) if item.get("found")
        }
    return {}


async def write_existing_vector_payload(
    workspace: str | Path,
    vector_store: Any,
    embedding_client: Any,
    *,
    source_ids: dict[str, dict[str, str]] | None = None,
    manifest_path: str | Path | None = None,
    routing: str | None = None,
    batch_size: int = 500,
    on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> set[str]:
    if batch_size < 1:
        raise ValueError("OCTX vector export batch size must be positive")
    if embedding_identity(embedding_client) is None:
        return set()
    import pyarrow as pa
    import pyarrow.ipc as ipc

    from sag_api.sag.octx_vector_manifest import VectorExportManifest

    root = Path(workspace)
    vectors_dir = root / "vectors"
    vectors_dir.mkdir(exist_ok=False)
    profiles: list[dict[str, Any]] = []
    written_roles: set[str] = set()
    lance_arrow_native = type(vector_store).__module__.endswith("lancedb_store")
    manifest = VectorExportManifest(manifest_path) if manifest_path is not None else None
    role_rows = _role_records(root) if manifest is None else {role: [] for role in ROLE_TARGETS}
    try:
        for role, rows in role_rows.items():
            role_sources = (source_ids or {}).get(role, {})
            total = manifest.count(role) if manifest is not None else len(rows)
            if total == 0 or (manifest is None and set(role_sources) != {record_id for record_id, _ in rows}):
                continue
            index, vector_field = ROLE_STORAGE[role]
            target = ROLE_TARGETS[role]
            output_path = vectors_dir / f"{target}.arrow"
            sink = pa.OSFile(str(output_path), "wb")
            writer = None
            schema = None
            dimension: int | None = None
            complete = True
            try:
                if lance_arrow_native:
                    rows_by_local_id = None if manifest is not None else {
                        role_sources[record_id]: (record_id, record)
                        for record_id, record in rows
                    }
                    completed = 0
                    vector_batches = _iter_lancedb_vector_batches(
                        vector_store,
                        index,
                        vector_field,
                        role=role,
                        routing=routing,
                        batch_size=batch_size,
                        manifest=manifest,
                        records_by_local_id=rows_by_local_id,
                    )
                    async for output_records, vector_values in vector_batches:
                        if on_progress is not None:
                            await on_progress(
                                {
                                    "phase": "vectors",
                                    "kind": role,
                                    "completed": completed,
                                    "total": total,
                                }
                            )
                        current_dimension = vector_values.type.list_size
                        if dimension is None:
                            dimension = current_dimension
                            schema = pa.schema(
                                [
                                    pa.field("record_id", pa.string(), nullable=False),
                                    pa.field("input_sha256", pa.string(), nullable=False),
                                    pa.field("vector", pa.list_(pa.float32(), dimension), nullable=False),
                                ]
                            )
                            writer = ipc.new_file(sink, schema)
                        if current_dimension != dimension:
                            complete = False
                            break
                        assert writer is not None and schema is not None
                        writer.write_batch(
                            pa.RecordBatch.from_arrays(
                                [
                                    pa.array([record_id for record_id, _ in output_records], type=pa.string()),
                                    pa.array([input_hash for _, input_hash in output_records], type=pa.string()),
                                    vector_values,
                                ],
                                schema=schema,
                            )
                        )
                        completed += len(output_records)
                        if on_progress is not None:
                            await on_progress(
                                {
                                    "phase": "vectors",
                                    "kind": role,
                                    "completed": completed,
                                    "total": total,
                                }
                            )
                        del vector_values, output_records
                        gc.collect()
                        pa.default_memory_pool().release_unused()
                    if completed != total:
                        complete = False
                else:
                    if manifest is not None:
                        batches = enumerate(manifest.iter_batches(role, batch_size=batch_size))
                    else:
                        batches = enumerate(
                            rows[offset : offset + batch_size]
                            for offset in range(0, len(rows), batch_size)
                        )
                    completed = 0
                    for _, raw_batch in batches:
                        if manifest is not None:
                            local_ids = [local_id for local_id, _, _ in raw_batch]
                            output_records = [(record_id, input_hash) for _, record_id, input_hash in raw_batch]
                            batch = None
                        else:
                            batch = raw_batch
                            local_ids = [role_sources[record_id] for record_id, _ in batch]
                            output_records = [
                                (record_id, input_sha256(render_role_input(role, record)))
                                for record_id, record in batch
                            ]
                        if on_progress is not None:
                            await on_progress(
                                {
                                    "phase": "vectors",
                                    "kind": role,
                                    "completed": completed,
                                    "total": total,
                                }
                            )
                        stored = await _fetch_vector_fields(
                            vector_store,
                            index,
                            local_ids,
                            [vector_field],
                            routing=routing,
                        )
                        if any(
                            local_id not in stored or stored[local_id].get(vector_field) is None
                            for local_id in local_ids
                        ):
                            complete = False
                            break
                        values = [_float_vector(stored[local_id][vector_field]) for local_id in local_ids]
                        current_dimension = len(values[0])
                        if dimension is None:
                            dimension = current_dimension
                            schema = pa.schema(
                                [
                                    pa.field("record_id", pa.string(), nullable=False),
                                    pa.field("input_sha256", pa.string(), nullable=False),
                                    pa.field("vector", pa.list_(pa.float32(), dimension), nullable=False),
                                ]
                            )
                            writer = ipc.new_file(sink, schema)
                        if current_dimension != dimension or any(len(vector) != dimension for vector in values):
                            complete = False
                            break
                        assert writer is not None and schema is not None
                        writer.write_batch(
                            pa.RecordBatch.from_arrays(
                                [
                                    pa.array([record_id for record_id, _ in output_records], type=pa.string()),
                                    pa.array([input_hash for _, input_hash in output_records], type=pa.string()),
                                    _vector_array(values, dimension=dimension),
                                ],
                                schema=schema,
                            )
                        )
                        completed += len(output_records)
                        if on_progress is not None:
                            await on_progress(
                                {
                                    "phase": "vectors",
                                    "kind": role,
                                    "completed": completed,
                                    "total": total,
                                }
                            )
                        del stored, values, output_records
                        gc.collect()
                        pa.default_memory_pool().release_unused()
                    if completed != total:
                        complete = False
            except Exception as error:
                complete = False
                logger.warning("OCTX vector role export skipped role=%s error=%s", role, error)
            finally:
                if writer is not None:
                    writer.close()
                sink.close()
            if not complete or dimension is None:
                output_path.unlink(missing_ok=True)
                continue
            profiles.append(vector_profile(role, embedding_client, dimension))
            written_roles.add(role)
    finally:
        if manifest is not None:
            manifest.close()
    if profiles:
        (vectors_dir / "profiles.json").write_text(
            json.dumps({"profiles": profiles}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    else:
        vectors_dir.rmdir()
    return written_roles
