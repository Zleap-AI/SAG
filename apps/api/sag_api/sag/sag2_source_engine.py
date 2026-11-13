"""Native SAG2 storage with the application's durable document-operation contract.

The application persists the same request envelope for both engines. This adapter
removes extraction options before calling SAG2 and maps its zero-event result back
to the shared publication DTO; no SAG1 engine or model clients are constructed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Column, MetaData, String, Table, delete, event, select, text
from zleap.sag.operations import OperationResult
from zleap.sag.pipeline import ChunkSetRef, EventSetRef
from zleap.sag2 import DataEngine, EngineConfig, JevConfig, RelationalConfig
from zleap.sag2.operations import (
    DeleteDataSourceRequest,
    DeleteSourceRequest,
    ProcessSourceRequest,
)
from zleap.sag2.pipeline.errors import PipelineCancelledError
from zleap.sag2.pipeline.options import IndexOptions

from sag_api.core.config import Settings
from sag_api.core.errors import ConfigurationError

_BINDINGS_METADATA = MetaData()
_BINDINGS = Table(
    "sag_app_sag2_inputs",
    _BINDINGS_METADATA,
    Column("namespace", String(256), primary_key=True),
    Column("source_config_id", String(64), nullable=False, index=True),
    Column("input_id", String(64), nullable=False),
)


def native_namespace(source_config_id: str, input_id: str) -> str:
    return uuid5(NAMESPACE_URL, f"sag:source:{source_config_id}:input:{input_id}").hex



def native_config(settings: Settings) -> EngineConfig:
    provider = settings.sag_relational_provider or "sqlite"
    if provider == "sqlite":
        relational = RelationalConfig(provider="sqlite", path=str(Path(settings.data_dir) / "sag2.db"))
    elif provider == "postgres":
        relational = RelationalConfig(
            provider="postgres",
            host=settings.sag_pg_host,
            port=settings.sag_pg_port,
            user=settings.sag_pg_user,
            password=settings.sag_pg_password,
            database=settings.sag_pg_database,
        )
    else:
        raise ConfigurationError("SAG2 仅支持 SQLite 与 PostgreSQL 关系库")
    jev = None
    if settings.jev_endpoint and settings.jev_api_key:
        jev = JevConfig(
            endpoint=settings.jev_endpoint,
            api_key=settings.jev_api_key,
            model=settings.jev_model,
            timeout_seconds=settings.sag2_decision_timeout_seconds,
            max_concurrency=settings.sag2_decision_max_concurrency,
        )
    return EngineConfig(relational=relational, jev=jev)


class Sag2SourceEngine:
    """One namespace in native SAG2 storage, independent of SAG1's tables."""

    def __init__(self, settings: Settings, source_config_id: str, *, scorer=None):
        self.source_config_id = source_config_id
        self.native = DataEngine(native_config(settings), health_check=False, scorer=scorer)

    async def start(self):
        if self.native.config.relational.provider == "postgres":
            sql_engine = self.native.store.engine
            async with sql_engine.begin() as connection:
                await connection.execute(text("CREATE SCHEMA IF NOT EXISTS sag2"))
            await sql_engine.dispose()

            @event.listens_for(sql_engine.sync_engine, "connect")
            def set_namespace(connection, _record):
                previous = connection.autocommit
                connection.autocommit = True
                try:
                    cursor = connection.cursor()
                    cursor.execute("SET SESSION search_path TO sag2")
                    cursor.close()
                finally:
                    connection.autocommit = previous

        await self.native.start()
        async with self.native.store.engine.begin() as connection:
            await connection.run_sync(_BINDINGS_METADATA.create_all)

    async def aclose(self):
        await self.native.aclose()

    def _result(self, result):
        if result is None:
            return None
        data = result.model_dump(mode="json")
        if data.get("failure_code") == "pipeline_cancelled":
            data["failure_code"] = "cancelled"
        data["data_source_id"] = self.source_config_id
        data["fence_scope"] = self.source_config_id
        payload = dict(data.get("result") or {})
        if data["operation_type"] == "process_source" and data["status"] == "succeeded":
            raw = {**payload, "data_source_id": self.source_config_id}
            chunks = ChunkSetRef.model_validate(
                {
                    **{key: value for key, value in raw.items() if key in ChunkSetRef.model_fields},
                    "vector_status": "skipped_by_policy",
                }
            )
            events = EventSetRef(
                data_source_id=chunks.data_source_id,
                source_type=chunks.source_type,
                source_id=chunks.source_id,
                source_version=chunks.source_version,
                chunk_version=chunks.chunk_version,
                extract_version="sag2:no-extraction",
                generation_id=chunks.generation_id,
                chunk_ids=chunks.chunk_ids,
                event_ids=(),
                relation_status="skipped_by_policy",
                vector_status="skipped_by_policy",
            )
            payload.update(chunk_ref=chunks.model_dump(mode="json"), event_ref=events.model_dump(mode="json"))
        if data["operation_type"] in {"delete_source", "delete_data_source"} and data["status"] == "succeeded":
            payload["target"] = "source" if data["operation_type"] == "delete_source" else "data_source"
        data["result"] = payload
        return OperationResult.model_validate(data)

    async def process_source(self, request, *, observer=None, cancellation=None):
        data = request.model_dump(mode="json", exclude={"extraction_options"})
        # SAG1's input-isolation flag is an app publication policy; SAG2 always
        # stores immutable generations and the app filters by the published input.
        data["context"].pop("isolation", None)
        input_id = data["source"]["descriptor"]["source_id"]
        namespace = native_namespace(self.source_config_id, input_id)
        data["source"]["descriptor"]["data_source_id"] = namespace
        data["context"]["fence_scope"] = namespace
        # Each app input is immutable and has its own fence, so processing or
        # resuming another document cannot invalidate this operation.
        async with self.native.store.engine.begin() as connection:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
            await connection.execute(
                insert(_BINDINGS)
                .values(
                    namespace=namespace,
                    source_config_id=self.source_config_id,
                    input_id=input_id,
                )
                .on_conflict_do_nothing()
            )
        data["index_options"] = {
            key: value for key, value in data.get("index_options", {}).items() if key in IndexOptions.model_fields
        }
        native_request = ProcessSourceRequest.model_validate(data)
        try:
            result = await self.native.process_source(native_request, observer=observer, cancellation=cancellation)
        except PipelineCancelledError:
            result = await self.native.get_operation_status(request.context.operation_id)
        return self._result(result)

    async def get_operation_status(self, operation_id):
        return self._result(await self.native.get_operation_status(operation_id))

    async def delete_source(self, request):
        data = request.model_dump(mode="json")
        data["context"].pop("isolation", None)
        namespace = native_namespace(self.source_config_id, request.source_id)
        data["data_source_id"] = namespace
        data["context"]["fence_scope"] = namespace
        data["context"]["fence_token"] = max(2, data["context"]["fence_token"])
        result = self._result(await self.native.delete_source(DeleteSourceRequest.model_validate(data)))
        return result.model_copy(update={
            "fence_token": request.context.fence_token, "fence_scope": request.context.fence_scope,
        })

    async def delete_data_source(self, request):
        async with self.native.store.engine.connect() as connection:
            namespaces = (
                (
                    await connection.execute(
                        select(_BINDINGS.c.namespace).where(
                            _BINDINGS.c.source_config_id == self.source_config_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        removed = 0
        for namespace in namespaces:
            data = request.model_dump(mode="json")
            data["context"].pop("isolation", None)
            data["context"].update(
                operation_id=request.context.operation_id + ":" + sha256(namespace.encode()).hexdigest()[:16],
                idempotency_key=request.context.idempotency_key + ":" + sha256(namespace.encode()).hexdigest()[:16],
                fence_scope=namespace,
                fence_token=max(3, request.context.fence_token),
            )
            data["data_source_id"] = namespace
            result = await self.native.delete_data_source(DeleteDataSourceRequest.model_validate(data))
            if result.status != "succeeded":
                return self._aggregate_delete_result(request, result=result)
            removed += int((result.result or {}).get("deleted_chunks", 0))
        async with self.native.store.engine.begin() as connection:
            await connection.execute(delete(_BINDINGS).where(_BINDINGS.c.source_config_id == self.source_config_id))
        return self._aggregate_delete_result(request, removed=removed)

    def _aggregate_delete_result(self, request, *, removed=0, result=None):
        now = datetime.now(UTC)
        return OperationResult(
            operation_id=request.context.operation_id,
            idempotency_key=request.context.idempotency_key,
            request_digest=request.context.request_digest,
            operation_type="delete_data_source",
            data_source_id=self.source_config_id,
            status=result.status if result is not None else "succeeded",
            checkpoint=result.checkpoint if result is not None else "ready",
            fence_scope=request.context.fence_scope,
            fence_token=request.context.fence_token,
            created_at=now, updated_at=now,
            failure_code=result.failure_code if result is not None else None,
            failure_message=result.failure_message if result is not None else None,
            retryable=result.retryable if result is not None else False,
            result={"target": "data_source", "deleted_chunks": removed},
        )

    async def read_chunk(self, source_id, chunk_id, *, data_source_id):
        return await self.native.read_chunk(
            source_id, chunk_id, data_source_id=native_namespace(data_source_id, source_id)
        )

    async def rows(self, input_ids):
        from zleap.sag2 import SearchScope

        if not input_ids:
            return []
        scope = SearchScope(data_source_ids=tuple(native_namespace(self.source_config_id, item) for item in input_ids))
        snapshot = await self.native.store.snapshot(scope)
        return list(snapshot.rows.values())
