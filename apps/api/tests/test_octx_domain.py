from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine


def test_octx_settings_enforce_production_resource_boundaries():
    """Removing a hard limit or accepting zero would make archive processing unbounded."""
    from sag_api.core.config import Settings

    configured = Settings(_env_file=None)

    assert configured.octx_max_upload_mb == 2048
    assert configured.octx_max_entries == 10_000
    assert configured.octx_max_file_mb == 512
    assert configured.octx_max_uncompressed_mb == 4096
    assert configured.octx_max_compression_ratio == 100
    assert configured.octx_max_jsonl_line_mb == 16
    assert configured.octx_max_jsonl_records == 1_000_000
    assert configured.octx_max_issues == 1000
    assert configured.octx_worker_memory_mb == 2048
    assert configured.octx_worker_timeout_seconds == 1800
    assert configured.octx_transfer_ttl_hours == 24
    assert configured.octx_rollback_retention_days == 7

    with pytest.raises(ValidationError):
        Settings(_env_file=None, octx_max_upload_mb=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, octx_max_compression_ratio=0)


def test_octx_enums_expose_stable_machine_values():
    """Renaming a persisted state or omitting a handler type would break recovery."""
    import sag_api.enums as enums

    required = {
        "OctxAssetOwnership": {"local", "imported"},
        "OctxInstallationStatus": {"shadow", "active", "retained", "gc", "failed"},
        "OctxTransferDirection": {"import", "export"},
        "OctxImportAction": {"update", "new", "cancel"},
        "OctxExportAction": {"export_ready_only", "cancel"},
    }
    for name, expected in required.items():
        assert hasattr(enums, name), f"missing enum {name}"
        assert {member.value for member in getattr(enums, name)} == expected

    transfer_states = {member.value for member in enums.OctxTransferStatus}
    assert transfer_states == {
        "uploaded",
        "validating",
        "decision_required",
        "queued",
        "importing",
        "indexing",
        "switching",
        "exporting",
        "packaging",
        "ready",
        "failed",
        "cancelled",
        "expired",
    }
    assert {
        enums.JobType.OCTX_PREFLIGHT.value,
        enums.JobType.OCTX_IMPORT.value,
        enums.JobType.OCTX_EXPORT.value,
        enums.JobType.OCTX_GC_INSTALLATION.value,
        enums.JobType.OCTX_GC_TRANSFER.value,
    } == {
        "octx_preflight",
        "octx_import",
        "octx_export",
        "octx_gc_installation",
        "octx_gc_transfer",
    }


def test_octx_export_decision_route_is_in_public_api_contract():
    """Without a decision endpoint, mixed-status export cannot be confirmed safely."""
    from sag_api.main import app

    operation = app.openapi()["paths"]["/api/v1/octx/exports/{transfer_id}/decision"]["post"]

    assert operation["responses"]["200"]
    assert operation["requestBody"]["required"] is True


def test_export_decision_response_exposes_excluded_documents():
    """A confirmation UI cannot be safe if the API hides which documents are excluded."""
    from datetime import UTC, datetime

    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.schemas.octx import OctxTransferOut

    now = datetime.now(UTC)
    transfer = SimpleNamespace(
        id="transfer",
        direction=OctxTransferDirection.EXPORT,
        status=OctxTransferStatus.DECISION_REQUIRED,
        progress=0.0,
        asset_id=None,
        release_id=None,
        package_version=None,
        package_digest=None,
        target_source_id="source",
        installation_id=None,
        validation_report=None,
        warnings=[],
        error=None,
        cancellation_requested=False,
        checkpoint={
            "allowed_actions": ["export_ready_only", "cancel"],
            "decision_token": "signed",
            "excluded_documents": [{"id": "doc", "filename": "busy.md", "status": "extracting"}],
        },
        created_at=now,
        updated_at=now,
    )

    response = OctxTransferOut.from_transfer(transfer)

    assert response.excluded_documents == [{"id": "doc", "filename": "busy.md", "status": "extracting"}]


def test_transfer_response_exposes_import_progress_detail():
    from datetime import UTC, datetime

    from sag_api.enums import OctxTransferDirection, OctxTransferStatus
    from sag_api.schemas.octx import OctxTransferOut

    now = datetime.now(UTC)
    transfer = SimpleNamespace(
        id="transfer-progress",
        direction=OctxTransferDirection.IMPORT,
        status=OctxTransferStatus.IMPORTING,
        progress=0.5,
        asset_id="asset",
        release_id="release",
        package_version="1.0.0",
        package_digest="digest",
        target_source_id=None,
        installation_id=None,
        validation_report=None,
        warnings=[],
        error=None,
        cancellation_requested=False,
        checkpoint={
            "progress_detail": {
                "phase": "rebuilding_documents",
                "completed_documents": 2,
                "total_documents": 4,
                "current_document": "knowledge/b.md",
            }
        },
        created_at=now,
        updated_at=now,
    )

    response = OctxTransferOut.from_transfer(transfer)

    assert response.progress_detail == transfer.checkpoint["progress_detail"]


def test_octx_error_taxonomy_is_machine_actionable():
    """Collapsing OCTX failures into generic errors would prevent safe retries and UI decisions."""
    from sag_api.core.error_taxonomy import ErrorCode, ErrorStage

    assert {
        "octx_invalid_package",
        "octx_validation_incomplete",
        "octx_resource_limit",
        "octx_unsupported_capability",
        "octx_rebuild_configuration_missing",
        "octx_release_digest_conflict",
        "octx_decision_required",
        "octx_decision_stale",
        "octx_local_changes_conflict",
        "octx_source_not_exportable",
        "octx_sag_mapping_conflict",
        "octx_shadow_validation_failed",
        "octx_transfer_cancelled",
        "octx_artifact_not_ready",
        "octx_artifact_missing",
    } <= {member.value for member in ErrorCode}
    assert {
        "octx_upload",
        "octx_validate",
        "octx_resolve",
        "octx_import",
        "octx_index",
        "octx_switch",
        "octx_export",
        "octx_publish",
    } <= {member.value for member in ErrorStage}


@pytest.mark.asyncio
async def test_octx_metadata_registers_identity_and_idempotency_constraints():
    """Dropping one identity constraint would allow mutable releases or duplicate installations."""
    import sag_api.db.models as models
    from sag_api.db.base import Base

    for name in (
        "OctxAsset",
        "OctxRelease",
        "OctxSourceBinding",
        "OctxInstallation",
        "OctxTransfer",
        "OctxOperationLease",
    ):
        assert hasattr(models, name), f"missing model {name}"

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        def _schema(sync_connection):
            schema = inspect(sync_connection)
            return {
                "tables": set(schema.get_table_names()),
                "release_unique": {
                    tuple(item["column_names"]) for item in schema.get_unique_constraints("octx_releases")
                },
                "installation_unique": {
                    tuple(item["column_names"]) for item in schema.get_unique_constraints("octx_installations")
                },
                "document_columns": {item["name"] for item in schema.get_columns("documents")},
                "document_indexes": {item["name"] for item in schema.get_indexes("documents")},
            }

        async with engine.connect() as connection:
            schema = await connection.run_sync(_schema)
    finally:
        await engine.dispose()

    assert {
        "octx_assets",
        "octx_releases",
        "octx_source_bindings",
        "octx_installations",
        "octx_transfers",
        "octx_operation_leases",
    } <= schema["tables"]
    assert ("asset_id", "version") in schema["release_unique"]
    assert ("asset_id", "package_digest") in schema["release_unique"]
    assert ("source_id", "release_id") in schema["installation_unique"]
    assert {"octx_installation_id", "octx_document_id", "is_active"} <= schema["document_columns"]
    assert "ix_documents_source_active_created" in schema["document_indexes"]


def test_transfer_state_machine_rejects_skips_and_terminal_reentry():
    """Allowing state skips could publish an unvalidated or partially indexed installation."""
    import sag_api.db.models.octx as domain
    from sag_api.enums import OctxTransferStatus

    assert hasattr(domain, "transition_transfer")
    transfer = SimpleNamespace(status=OctxTransferStatus.UPLOADED)

    domain.transition_transfer(transfer, OctxTransferStatus.VALIDATING)
    domain.transition_transfer(transfer, OctxTransferStatus.DECISION_REQUIRED)
    domain.transition_transfer(transfer, OctxTransferStatus.QUEUED)
    domain.transition_transfer(transfer, OctxTransferStatus.IMPORTING)
    domain.transition_transfer(transfer, OctxTransferStatus.INDEXING)
    domain.transition_transfer(transfer, OctxTransferStatus.SWITCHING)
    domain.transition_transfer(transfer, OctxTransferStatus.READY)
    assert transfer.status is OctxTransferStatus.READY

    with pytest.raises(ValueError, match="ready.*queued"):
        domain.transition_transfer(transfer, OctxTransferStatus.QUEUED)

    invalid = SimpleNamespace(status=OctxTransferStatus.UPLOADED)
    with pytest.raises(ValueError, match="uploaded.*switching"):
        domain.transition_transfer(invalid, OctxTransferStatus.SWITCHING)


@pytest.mark.asyncio
async def test_existing_sqlite_documents_gain_octx_columns_and_active_index(monkeypatch):
    """A create_all-only implementation would leave upgraded databases unusable."""
    import sag_api.core.db as database

    old_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(database, "engine", old_engine)
    try:
        async with old_engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE documents ("
                    "id VARCHAR(32) PRIMARY KEY, source_id VARCHAR(32) NOT NULL, "
                    "sag_source_id VARCHAR(64), created_at DATETIME NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE messages ("
                    "id VARCHAR(32) PRIMARY KEY, thread_id VARCHAR(32) NOT NULL, "
                    "created_at DATETIME NOT NULL)"
                )
            )

        await database._ensure_columns()
        await database._ensure_indexes()

        def _upgraded(sync_connection):
            schema = inspect(sync_connection)
            return (
                {item["name"] for item in schema.get_columns("documents")},
                {item["name"] for item in schema.get_indexes("documents")},
            )

        async with old_engine.connect() as connection:
            columns, indexes = await connection.run_sync(_upgraded)
    finally:
        await old_engine.dispose()

    assert {"octx_installation_id", "octx_document_id", "is_active"} <= columns
    assert "ix_documents_source_active_created" in indexes


def test_incremental_boolean_ddl_is_portable_to_postgresql():
    """Existing PostgreSQL deployments reject integer defaults for BOOLEAN columns."""
    from sag_api.core.db import _COLUMN_UPGRADES

    boolean_ddls = [
        ddl
        for columns in _COLUMN_UPGRADES.values()
        for ddl in columns.values()
        if ddl.startswith("BOOLEAN")
    ]

    assert boolean_ddls
    assert all("DEFAULT 0" not in ddl and "DEFAULT 1" not in ddl for ddl in boolean_ddls)
