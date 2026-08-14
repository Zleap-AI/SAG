from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from sag_api.enums import (
    OctxExportAction,
    OctxImportAction,
    OctxTransferDirection,
    OctxTransferStatus,
)


class OctxImportDecisionIn(BaseModel):
    action: OctxImportAction
    target_source_id: str | None = None
    discard_local_changes: bool = False
    decision_token: str


class OctxExportCreate(BaseModel):
    version: str | None = Field(default=None, max_length=64)


class OctxExportDecisionIn(BaseModel):
    action: OctxExportAction
    decision_token: str = Field(min_length=1)


class OctxTransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    direction: OctxTransferDirection
    status: OctxTransferStatus
    progress: float
    asset: dict | None = None
    release: dict | None = None
    target_source_id: str | None = None
    installation_id: str | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    decision_token: str | None = None
    conflicts: list[dict] = Field(default_factory=list)
    excluded_documents: list[dict] = Field(default_factory=list)
    record_counts: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    progress_detail: dict = Field(default_factory=dict)
    export_scope: str = "source"
    document_id: str | None = None
    document_name: str | None = None
    validation_report: dict | None = None
    warnings: list = Field(default_factory=list)
    error: dict | None = None
    cancellation_requested: bool = False
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_transfer(cls, transfer) -> OctxTransferOut:  # noqa: ANN001
        checkpoint = dict(transfer.checkpoint or {})
        return cls(
            id=transfer.id,
            direction=transfer.direction,
            status=transfer.status,
            progress=transfer.progress,
            asset=({"id": transfer.asset_id, "name": checkpoint.get("asset_name")} if transfer.asset_id else None),
            release=(
                {
                    "id": transfer.release_id,
                    "version": transfer.package_version,
                    "package_digest": transfer.package_digest,
                }
                if transfer.release_id or transfer.package_version
                else None
            ),
            target_source_id=transfer.target_source_id,
            installation_id=transfer.installation_id,
            allowed_actions=list(checkpoint.get("allowed_actions") or []),
            decision_token=checkpoint.get("decision_token"),
            conflicts=list(checkpoint.get("conflicts") or []),
            excluded_documents=list(checkpoint.get("excluded_documents") or []),
            record_counts=dict(checkpoint.get("record_counts") or {}),
            capabilities=dict(checkpoint.get("capabilities") or {}),
            progress_detail=dict(checkpoint.get("progress_detail") or {}),
            export_scope=str(checkpoint.get("export_scope") or "source"),
            document_id=checkpoint.get("document_id"),
            document_name=checkpoint.get("document_name"),
            validation_report=transfer.validation_report,
            warnings=list(transfer.warnings or []),
            error=transfer.error,
            cancellation_requested=transfer.cancellation_requested,
            created_at=transfer.created_at,
            updated_at=transfer.updated_at,
        )
