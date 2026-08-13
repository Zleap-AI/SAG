from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from packaging.version import InvalidVersion, Version
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.config import settings
from sag_api.core.error_taxonomy import ErrorCode, ErrorLayer, ErrorStage
from sag_api.core.errors import ConflictError, NotFoundError, ValidationError
from sag_api.db.models import OctxRelease, OctxSourceBinding, OctxTransfer, Source
from sag_api.db.models.octx import transition_transfer
from sag_api.enums import OctxImportAction, OctxTransferStatus
from sag_api.octx.decision_token import DecisionTokenError, verify_decision_token


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    source_id: str
    source_name: str
    active_version: str
    content_revision: int
    local_changes: bool


@dataclass(frozen=True, slots=True)
class ConflictResolution:
    kind: Literal["new", "idempotent", "decision_required"]
    asset_id: str
    version: str
    package_digest: str
    allowed_actions: tuple[str, ...]
    source_id: str | None = None
    conflicts: tuple[ConflictCandidate, ...] = ()
    highest_version: str | None = None


@dataclass(frozen=True, slots=True)
class ImportDecision:
    action: OctxImportAction | str
    decision_token: str
    target_source_id: str | None = None
    discard_local_changes: bool = False


def _identity(validated: Any) -> tuple[str, str, str]:
    try:
        asset_id = str(validated.manifest["asset"]["id"])
        release = validated.manifest["release"]
        version = str(release["version"])
        digest = str(release["package_digest"])
        Version(version)
    except (KeyError, TypeError, InvalidVersion) as error:
        raise ValidationError(
            "OCTX manifest has invalid asset/release identity",
            code=ErrorCode.OCTX_INVALID_PACKAGE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
        ) from error
    return asset_id, version, digest


async def resolve_import_conflict(
    session: AsyncSession, validated: Any
) -> ConflictResolution:
    asset_id, version, digest = _identity(validated)
    releases = (
        (
            await session.execute(
                select(OctxRelease).where(OctxRelease.asset_id == asset_id)
            )
        )
        .scalars()
        .all()
    )
    if not releases:
        return ConflictResolution(
            "new", asset_id, version, digest, ("new", "cancel")
        )

    same_version = next((item for item in releases if item.version == version), None)
    if same_version is not None and same_version.package_digest != digest:
        raise ConflictError(
            "same OCTX Asset/version has a different package digest",
            code=ErrorCode.OCTX_RELEASE_DIGEST_CONFLICT,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
            retryable=False,
        )

    if same_version is not None:
        binding = await session.scalar(
            select(OctxSourceBinding).where(
                OctxSourceBinding.asset_id == asset_id,
                OctxSourceBinding.active_release_id == same_version.id,
            )
        )
        if binding is not None:
            return ConflictResolution(
                "idempotent",
                asset_id,
                version,
                digest,
                (),
                source_id=binding.source_id,
                highest_version=max(releases, key=lambda item: Version(item.version)).version,
            )

    rows = (
        await session.execute(
            select(OctxSourceBinding, Source, OctxRelease)
            .join(Source, Source.id == OctxSourceBinding.source_id)
            .join(OctxRelease, OctxRelease.id == OctxSourceBinding.active_release_id)
            .where(OctxSourceBinding.asset_id == asset_id)
            .order_by(Source.id)
        )
    ).all()
    highest = max(releases, key=lambda item: Version(item.version)).version
    conflicts = tuple(
        ConflictCandidate(
            source_id=source.id,
            source_name=source.name,
            active_version=release.version,
            content_revision=binding.content_revision,
            local_changes=binding.content_revision != binding.released_revision,
        )
        for binding, source, release in rows
    )
    actions = (
        ("update", "new", "cancel")
        if conflicts and Version(version) > Version(highest)
        else ("new", "cancel")
    )
    return ConflictResolution(
        "decision_required" if conflicts else "new",
        asset_id,
        version,
        digest,
        actions,
        conflicts=conflicts,
        highest_version=highest,
    )


def _stale(message: str) -> ConflictError:
    return ConflictError(
        message,
        code=ErrorCode.OCTX_DECISION_STALE,
        layer=ErrorLayer.API,
        stage=ErrorStage.OCTX_RESOLVE,
        retryable=False,
    )


async def confirm_import_decision(
    session: AsyncSession,
    transfer_id: str,
    decision: ImportDecision,
    *,
    secret: str | None = None,
) -> OctxTransfer:
    transfer = await session.get(OctxTransfer, transfer_id)
    if transfer is None:
        raise NotFoundError("OCTX transfer not found")
    if transfer.status is not OctxTransferStatus.DECISION_REQUIRED:
        raise ConflictError("OCTX transfer is not waiting for a decision")

    try:
        claims = verify_decision_token(
            decision.decision_token, secret=secret or settings.secret_key
        )
    except DecisionTokenError as error:
        transition_transfer(transfer, OctxTransferStatus.VALIDATING)
        await session.flush()
        raise _stale(str(error)) from error
    if claims.transfer_id != transfer.id or claims.asset_id != transfer.asset_id:
        transition_transfer(transfer, OctxTransferStatus.VALIDATING)
        await session.flush()
        raise _stale("OCTX decision token does not match transfer")

    try:
        action = OctxImportAction(decision.action)
    except ValueError as error:
        raise ValidationError("invalid OCTX import decision action") from error
    allowed_actions = set((transfer.checkpoint or {}).get("allowed_actions") or ())
    if allowed_actions and action.value not in allowed_actions:
        raise ConflictError(
            f"OCTX import action is not allowed: {action.value}",
            code=ErrorCode.OCTX_DECISION_STALE,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
            retryable=False,
        )

    if action is OctxImportAction.CANCEL:
        transfer.selected_action = action
        transition_transfer(transfer, OctxTransferStatus.CANCELLED)
        await session.flush()
        return transfer
    if action is OctxImportAction.NEW:
        if decision.target_source_id is not None:
            raise ValidationError("new OCTX import must not specify target_source_id")
        transfer.selected_action = action
        transfer.target_source_id = None
        transition_transfer(transfer, OctxTransferStatus.QUEUED)
        await session.flush()
        return transfer

    target_source_id = decision.target_source_id
    if target_source_id is None or target_source_id not in claims.source_revisions:
        raise ValidationError("update target is not present in OCTX decision token")
    binding = await session.get(OctxSourceBinding, target_source_id)
    if binding is None or binding.asset_id != claims.asset_id:
        transition_transfer(transfer, OctxTransferStatus.VALIDATING)
        await session.flush()
        raise _stale("OCTX update target binding changed")
    if binding.content_revision != claims.source_revisions[target_source_id]:
        transition_transfer(transfer, OctxTransferStatus.VALIDATING)
        await session.flush()
        raise _stale("OCTX source content revision changed")
    if (
        binding.content_revision != binding.released_revision
        and not decision.discard_local_changes
    ):
        raise ConflictError(
            "OCTX target source has unpublished local changes",
            code=ErrorCode.OCTX_LOCAL_CHANGES_CONFLICT,
            layer=ErrorLayer.API,
            stage=ErrorStage.OCTX_RESOLVE,
            retryable=False,
        )

    transfer.selected_action = action
    transfer.target_source_id = target_source_id
    transfer.checkpoint = {
        **dict(transfer.checkpoint or {}),
        "expected_source_revision": claims.source_revisions[target_source_id],
    }
    transition_transfer(transfer, OctxTransferStatus.QUEUED)
    await session.flush()
    return transfer
