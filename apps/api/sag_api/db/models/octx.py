from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from sqlalchemy import JSON, BigInteger, Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from sag_api.db.base import Base, IDMixin, TimestampMixin, UTCDateTime
from sag_api.enums import (
    OctxAssetOwnership,
    OctxImportAction,
    OctxInstallationStatus,
    OctxReleaseOrigin,
    OctxTransferDirection,
    OctxTransferStatus,
)


def _enum_type(enum_cls: type[StrEnum], *, length: int) -> SAEnum:
    """Persist public enum values, never Python member names."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
    )


class OctxAsset(TimestampMixin, Base):
    __tablename__ = "octx_assets"

    # OCTX exchange identity is UUIDv7 and must not be converted to a SAG ID.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ownership: Mapped[OctxAssetOwnership] = mapped_column(
        _enum_type(OctxAssetOwnership, length=16)
    )
    producer_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )


class OctxRelease(IDMixin, TimestampMixin, Base):
    __tablename__ = "octx_releases"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_octx_release_asset_version"),
        UniqueConstraint(
            "asset_id", "package_digest", name="uq_octx_release_asset_digest"
        ),
    )

    asset_id: Mapped[str] = mapped_column(
        ForeignKey("octx_assets.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[str] = mapped_column(String(64))
    package_digest: Mapped[str] = mapped_column(String(71))
    manifest: Mapped[dict] = mapped_column("manifest_json", JSON)
    artifact_key: Mapped[str] = mapped_column(String(1024))
    created_by: Mapped[OctxReleaseOrigin] = mapped_column(
        _enum_type(OctxReleaseOrigin, length=16)
    )


class OctxSourceBinding(TimestampMixin, Base):
    __tablename__ = "octx_source_bindings"

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("octx_assets.id", ondelete="RESTRICT"), index=True
    )
    active_release_id: Mapped[str] = mapped_column(
        ForeignKey("octx_releases.id", ondelete="RESTRICT")
    )
    content_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    released_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    workspace_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class OctxInstallation(IDMixin, TimestampMixin, Base):
    __tablename__ = "octx_installations"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "release_id", name="uq_octx_installation_source_release"
        ),
    )

    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )
    release_id: Mapped[str] = mapped_column(
        ForeignKey("octx_releases.id", ondelete="RESTRICT"), index=True
    )
    sag_source_config_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    id_namespace: Mapped[str] = mapped_column(String(36))
    status: Mapped[OctxInstallationStatus] = mapped_column(
        _enum_type(OctxInstallationStatus, length=16),
        default=OctxInstallationStatus.SHADOW,
        index=True,
    )
    counts: Mapped[dict] = mapped_column("counts_json", JSON, default=dict)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    retain_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OctxTransfer(IDMixin, TimestampMixin, Base):
    __tablename__ = "octx_transfers"

    direction: Mapped[OctxTransferDirection] = mapped_column(
        _enum_type(OctxTransferDirection, length=16), index=True
    )
    status: Mapped[OctxTransferStatus] = mapped_column(
        _enum_type(OctxTransferStatus, length=24),
        default=OctxTransferStatus.UPLOADED,
        index=True,
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    upload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    input_signature: Mapped[dict | None] = mapped_column("input_signature_json", JSON, nullable=True)
    staging_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("octx_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("octx_releases.id", ondelete="SET NULL"), nullable=True, index=True
    )
    installation_id: Mapped[str | None] = mapped_column(
        ForeignKey("octx_installations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    selected_action: Mapped[OctxImportAction | None] = mapped_column(
        _enum_type(OctxImportAction, length=16), nullable=True
    )
    package_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_digest: Mapped[str | None] = mapped_column(String(71), nullable=True)
    validation_report: Mapped[dict | None] = mapped_column(
        "validation_report_json", JSON, nullable=True
    )
    warnings: Mapped[list] = mapped_column("warnings_json", JSON, default=list)
    error: Mapped[dict | None] = mapped_column("error_json", JSON, nullable=True)
    checkpoint: Mapped[dict] = mapped_column("checkpoint_json", JSON, default=dict)
    cancellation_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    decision_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )


class OctxOperationLease(TimestampMixin, Base):
    __tablename__ = "octx_operation_leases"

    resource_key: Mapped[str] = mapped_column(String(256), primary_key=True)
    owner_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime())


_TRANSFER_TRANSITIONS: dict[OctxTransferStatus, frozenset[OctxTransferStatus]] = {
    OctxTransferStatus.UPLOADED: frozenset(
        {
            OctxTransferStatus.VALIDATING,
            OctxTransferStatus.CANCELLED,
            OctxTransferStatus.EXPIRED,
        }
    ),
    OctxTransferStatus.VALIDATING: frozenset(
        {
            OctxTransferStatus.DECISION_REQUIRED,
            OctxTransferStatus.QUEUED,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.DECISION_REQUIRED: frozenset(
        {
            OctxTransferStatus.VALIDATING,
            OctxTransferStatus.QUEUED,
            OctxTransferStatus.CANCELLED,
            OctxTransferStatus.EXPIRED,
        }
    ),
    OctxTransferStatus.QUEUED: frozenset(
        {
            OctxTransferStatus.IMPORTING,
            OctxTransferStatus.EXPORTING,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.IMPORTING: frozenset(
        {
            OctxTransferStatus.INDEXING,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.INDEXING: frozenset(
        {
            OctxTransferStatus.SWITCHING,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.SWITCHING: frozenset(
        {OctxTransferStatus.READY, OctxTransferStatus.FAILED}
    ),
    OctxTransferStatus.EXPORTING: frozenset(
        {
            OctxTransferStatus.PACKAGING,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.PACKAGING: frozenset(
        {
            OctxTransferStatus.READY,
            OctxTransferStatus.FAILED,
            OctxTransferStatus.CANCELLED,
        }
    ),
    OctxTransferStatus.READY: frozenset(),
    OctxTransferStatus.FAILED: frozenset(),
    OctxTransferStatus.CANCELLED: frozenset(),
    OctxTransferStatus.EXPIRED: frozenset(),
}


class _TransferState(Protocol):
    status: OctxTransferStatus | str


class _InstallationState(Protocol):
    status: OctxInstallationStatus | str


def transition_transfer(transfer: _TransferState, target: OctxTransferStatus) -> None:
    """Apply one legal transfer transition; repeating the current state is idempotent."""
    current = OctxTransferStatus(transfer.status)
    target = OctxTransferStatus(target)
    if current is target:
        return
    if target not in _TRANSFER_TRANSITIONS[current]:
        raise ValueError(f"illegal OCTX transfer transition: {current.value} -> {target.value}")
    transfer.status = target


_INSTALLATION_TRANSITIONS: dict[
    OctxInstallationStatus, frozenset[OctxInstallationStatus]
] = {
    OctxInstallationStatus.SHADOW: frozenset(
        {OctxInstallationStatus.ACTIVE, OctxInstallationStatus.FAILED}
    ),
    OctxInstallationStatus.ACTIVE: frozenset({OctxInstallationStatus.RETAINED}),
    OctxInstallationStatus.RETAINED: frozenset({OctxInstallationStatus.GC}),
    OctxInstallationStatus.FAILED: frozenset({OctxInstallationStatus.GC}),
    OctxInstallationStatus.GC: frozenset(),
}


def transition_installation(
    installation: _InstallationState, target: OctxInstallationStatus
) -> None:
    current = OctxInstallationStatus(installation.status)
    target = OctxInstallationStatus(target)
    if current is target:
        return
    if target not in _INSTALLATION_TRANSITIONS[current]:
        raise ValueError(
            f"illegal OCTX installation transition: {current.value} -> {target.value}"
        )
    installation.status = target
