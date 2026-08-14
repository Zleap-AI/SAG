from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sag_api.db.models import Document, FnOSNasLegacyFolder, Job, Source
from sag_api.enums import ConnectorKind, DocumentStatus, JobType, SourceType
from sag_api.schemas.document import DocumentOut
from sag_api.schemas.fnos_nas import NasImportProgressOut, NasImportRequest


def _source() -> Source:
    return Source(
        name="Private documents",
        source_type=SourceType.DOCUMENT,
        connector_kind=ConnectorKind.FILE_UPLOAD,
        sag_source_config_id="source-config",
    )


def _document(source_id: str, filename: str, **origin: object) -> Document:
    return Document(
        source_id=source_id,
        filename=filename,
        content_type="application/pdf",
        size_bytes=100,
        storage_path=f"/private/{filename}",
        status=DocumentStatus.PENDING,
        chunk_count=0,
        event_count=0,
        progress=0,
        token_usage=0,
        **origin,
    )


@pytest.mark.asyncio
async def test_document_origin_uniqueness_allows_unlimited_null_upload_origins() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        source = _source()
        session.add(source)
        await session.flush()
        session.add_all([_document(source.id, "one.pdf"), _document(source.id, "two.pdf")])
        await session.commit()

        first = _document(
            source.id,
            "nas-one.pdf",
            origin_kind="fnos_shared",
            origin_key="a" * 64,
            origin_path="/vol1/docs/one.pdf",
            origin_display_path="Documents/one.pdf",
            origin_size_bytes=100,
            origin_mtime_ns=1,
            origin_sha256="b" * 64,
        )
        session.add(first)
        await session.commit()

        session.add(
            _document(
                source.id,
                "duplicate.pdf",
                origin_kind="fnos_shared",
                origin_key="a" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        assert len((await session.scalars(select(Document))).all()) == 3

    await engine.dispose()


def test_document_out_exposes_only_safe_origin_metadata() -> None:
    now = datetime.now(UTC)
    document = _document(
        "source-id",
        "handbook.pdf",
        origin_kind="fnos_shared",
        origin_key="a" * 64,
        origin_path="/vol1/private/handbook.pdf",
        origin_display_path="Policies/handbook.pdf",
        origin_size_bytes=100,
        origin_mtime_ns=1,
        origin_sha256="b" * 64,
    )
    document.id = "document-id"
    document.created_at = now
    document.updated_at = now

    output = DocumentOut.model_validate(document).model_dump()

    assert output["origin_kind"] == "fnos_shared"
    assert output["origin_display_path"] == "Policies/handbook.pdf"
    assert "origin_key" not in DocumentOut.model_fields
    assert "origin_path" not in DocumentOut.model_fields
    assert "origin_size_bytes" not in DocumentOut.model_fields
    assert "origin_mtime_ns" not in DocumentOut.model_fields
    assert "origin_sha256" not in DocumentOut.model_fields
    assert "/vol1" not in str(output)


@pytest.mark.asyncio
async def test_import_job_enum_and_legacy_folder_roundtrip() -> None:
    assert JobType.IMPORT_NAS_DOCUMENTS.value == "import_nas_documents"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Source.__table__.create)
        await connection.run_sync(Document.__table__.create)
        await connection.run_sync(Job.__table__.create)
        await connection.run_sync(FnOSNasLegacyFolder.__table__.create)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        folder = FnOSNasLegacyFolder(path="/vol1/1000/Documents", display_label="My documents")
        job = Job(type=JobType.IMPORT_NAS_DOCUMENTS, payload={"entries": []})
        session.add_all([folder, job])
        await session.commit()
        session.expunge_all()

        loaded_job = await session.get(Job, job.id)
        loaded_folder = await session.get(FnOSNasLegacyFolder, folder.id)
        assert loaded_job is not None and loaded_job.type is JobType.IMPORT_NAS_DOCUMENTS
        assert loaded_folder is not None and loaded_folder.path == "/vol1/1000/Documents"

    await engine.dispose()


def test_nas_import_schemas_forbid_private_fields_and_bound_selection_count() -> None:
    request = NasImportRequest(source_id="source-id", selection_tokens=["opaque-token"])
    assert request.selection_tokens == ["opaque-token"]

    with pytest.raises(ValidationError):
        NasImportRequest(source_id="source-id", selection_tokens=["token"] * 501)
    with pytest.raises(ValidationError):
        NasImportRequest(source_id="source-id", selection_tokens=["x" * 2049])
    with pytest.raises(ValidationError):
        NasImportRequest(source_id="source-id", selection_tokens=["token"], origin_path="/vol1/private")

    assert "payload" not in NasImportProgressOut.model_fields
    assert "origin_path" not in NasImportProgressOut.model_fields
    assert "selection_tokens" not in NasImportProgressOut.model_fields
