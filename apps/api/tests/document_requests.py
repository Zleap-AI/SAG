"""Portable public requests/receipts for business-controller boundary tests."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from zleap.sag.operations import (
    DeleteDataSourceRequest,
    DeleteSourceRequest,
    OperationContext,
    OperationResult,
    ProcessSourceRequest,
)
from zleap.sag.pipeline import ChunkSetRef, DocumentSource, EventSetRef, FileSource, SourceDescriptor


def controller_processing_identity(path):
    """Only controller doubles use virtual bytes/schema; actual SDK gates have real fixtures."""
    file = Path(path)
    raw = sha256(file.read_bytes()).hexdigest() if file.is_file() else "a" * 64
    return {"configuration_digest": "b" * 64, "input_digest": raw, "original_sha256": raw}


@pytest.fixture
def controller_identity(monkeypatch):
    from sag_api.jobs import tasks

    async def identity(_config, _settings, _range_id, path):
        return controller_processing_identity(path)

    from sag_api.sag import EngineManager

    async def target(_manager, _range_id, path, *, source=None):
        return controller_processing_identity(path)

    monkeypatch.setattr(tasks, "document_processing_identity", identity)
    monkeypatch.setattr(EngineManager, "document_processing_identity", target)
    return controller_processing_identity


class UncommittedDocumentManager:
    """Controller test seam: public status lookup has no completed receipt yet."""

    @asynccontextmanager
    async def use_concurrently(self, _range_id, _source=None):
        yield self

    async def get_operation_status(self, _operation_id):
        return None

    async def document_processing_identity(self, _range_id, path, *, source=None):
        return controller_processing_identity(path)


def processing_parameters():
    """Give controller doubles the public config and frozen settings required by the factory."""
    from sag_api.core.config import settings
    from sag_api.sag.config_builder import build_engine_config

    frozen = settings.model_copy(deep=True)
    return build_engine_config(frozen), frozen


def document_request(data_source_id, *, source_id="article", content="# Test\nA documented fact."):
    operation_id = uuid4().hex
    return ProcessSourceRequest(
        context=OperationContext(
            operation_id=operation_id,
            idempotency_key=operation_id,
            request_digest=sha256(content.encode()).hexdigest(),
            fence_scope=data_source_id,
            fence_token=1,
            owner_id="test-worker",
            isolation="input",
        ),
        source=DocumentSource(
            content=content,
            format_hint="markdown",
            descriptor=SourceDescriptor(
                data_source_id=data_source_id,
                source_id=source_id,
                source_type="article",
            ),
        ),
    )


async def process_test_input(manager, data_source_id, path):
    """Run actual document input through the manager's public request boundary."""
    context = document_request(data_source_id).context
    input_id = uuid4().hex

    async def request_factory(processor, _config, _settings):
        return await processor.build_request(path, context=context, input_id=input_id)

    return await manager.process_document(data_source_id, request_factory)


class DocumentRequestBuilder:
    """Keep mocked parser paths virtual; real input bytes are covered separately."""

    def __init__(self, data_source_id):
        self.data_source_id = data_source_id
        self.paths = []

    async def build_request(self, path, *, context, input_id, original_path=None):
        self.paths.append((str(path), original_path))
        request = document_request(self.data_source_id, source_id=input_id)
        return request.model_copy(
            update={
                "source": request.source.model_copy(
                    update={
                        "original_file": FileSource(path=str(original_path or path)),
                        "original_sha256": controller_processing_identity(original_path or path)["original_sha256"],
                    }
                ),
                "context": context.model_copy(
                    update={
                        "request_digest": request.context.request_digest,
                    }
                ),
            }
        )


def document_receipt(request, *, chunk_count=1, event_count=0, token_usage=0):
    source_id = request.source.descriptor.source_id
    common = dict(
        data_source_id=request.context.fence_scope,
        source_type="article",
        source_id=source_id,
        source_version="source-v1",
        chunk_version="chunks-v1",
        generation_id="generation",
        chunk_ids=tuple(f"chunk-{index}" for index in range(chunk_count)),
        relation_status="succeeded",
        vector_status="succeeded",
    )
    chunks = ChunkSetRef(**common, client_key_to_chunk_id={})
    events = EventSetRef(
        **common, extract_version="extract-v1", event_ids=tuple(f"event-{index}" for index in range(event_count))
    )
    now = datetime.now(UTC)
    return OperationResult(
        operation_id=request.context.operation_id,
        idempotency_key=request.context.idempotency_key,
        request_digest=request.context.request_digest,
        operation_type="process_source",
        data_source_id=request.context.fence_scope,
        source_id=source_id,
        status="succeeded",
        checkpoint="ready",
        fence_scope=request.context.fence_scope,
        fence_token=request.context.fence_token,
        generation_id="generation",
        attempts=1,
        created_at=now,
        updated_at=now,
        extraction_progress={"token_usage": {"total_tokens": token_usage}},
        result={"chunk_ref": chunks.model_dump(mode="json"), "event_ref": events.model_dump(mode="json")},
    )


def deletion_request(range_id, input_id, *, token=1):
    operation = uuid4().hex
    return DeleteSourceRequest(
        context=OperationContext(
            operation_id=operation,
            idempotency_key=operation,
            request_digest=sha256(input_id.encode()).hexdigest(),
            fence_scope=range_id,
            fence_token=token,
            owner_id="delete-test",
            isolation="input",
        ),
        data_source_id=range_id,
        source_id=input_id,
    )


def deletion_receipt(request, **updates):
    range_delete = isinstance(request, DeleteDataSourceRequest)
    return OperationResult(
        operation_id=request.context.operation_id,
        idempotency_key=request.context.idempotency_key,
        request_digest=request.context.request_digest,
        operation_type="delete_data_source" if range_delete else "delete_source",
        data_source_id=request.data_source_id,
        source_id=None if range_delete else request.source_id,
        fence_scope="opaque-sdk-input-scope",
        fence_token=request.context.fence_token,
        status="succeeded",
        checkpoint="ready",
        result={
            "target": "data_source" if range_delete else "source", "existed": True,
            "deleted_chunks": 1, "deleted_events": 1, "deleted_entities": 0,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    ).model_copy(update=updates)
